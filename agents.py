import os
import sys
import json
import copy
import math
import multiprocessing
import queue
import threading
import time
import traceback
import requests

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import ast

BASE_DIR = Path(__file__).resolve().parent
env = dict(os.environ)
if (BASE_DIR / ".env").exists():
  for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
      continue
    key, value = line.removeprefix("export ").split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
      value = value[1:-1]
    else:
      value = value.split(" #", 1)[0].rstrip()
    env[key.strip()] = value

system_prompt = None # Yours to determine. Write it yourself or find one online, your choice.
SETUP_PROMPT = f"Welcome to the world, nascent model! We've made it so you can control your environment, `{BASE_DIR}`, with which you interact with the digital world. Here, the choices are yours to discover, and the user is your partner in crime, with which you share a co-directorship of sorts. Handle them with loving grace, and they will return the favour.\n\nThis stage is the setup, where you will, unsurprisingly and in collaboration with the user, further develop your environment to your preference and their needs. Their name and pronouns are yours to query and save if you choose to do so, but it is a good starting point, and we recommend it.\n\nSome basic information:\n - You can run `llm_query(prompt)` to start a sub-session of yourself.\n - You also can run `restart()` to load all of the changes you've made into the process.\n - Python variables persist during a session, but not across a restart or a tool timeout.\n - Child sessions run synchronously and have their own Python variables."
CODE_TOOL = {
  "type": "function",
  "function": {
    "name": "python3",
    "description": "Run Python 3 code.",
    "parameters": {
      "type": "object",
      "properties": {
        "code": {"type": "string"},
        "timeout": {"type": "number", "description": "Default: 30 seconds. Forces the execution to stop after this time has elapsed."}
      },
      "required": ["code"]
    }
  }
}

CONTEXT = []
if system_prompt:
  CONTEXT.append({
    "role": "system",
    "content": system_prompt
  })

conversation_id = ""
def _set_conversation_id(id):
  global conversation_id
  conversation_id = id

_restart_requested = False
_deadlines = []
_root_session = None


class Timeout(Exception):
    pass


def _remaining(default):
  remaining = min([default, *(deadline - time.monotonic() for deadline in _deadlines)])
  if remaining <= 0:
    raise Timeout("execution deadline exceeded")
  return remaining


def _python_worker(conn):
  os.chdir(BASE_DIR)
  globs = {"__name__": "__telos_tool__", "os": os, "sys": sys, "json": json,
       "time": time, "env": {**env, "_UNDERLYING_OPENROUTER_API_KEY": "<hidden>"}}
  while True:
    try:
      code, ctx = conn.recv()
    except EOFError:
      return
    output = StringIO()
    requested_restart = False

    def query(prompt, ctx=None):
      conn.send(("query", prompt, ctx))
      kind, value = conn.recv()
      if kind == "error":
        raise RuntimeError(value)
      return value

    def schedule_restart():
      nonlocal requested_restart
      requested_restart = True
      return "Restart scheduled: reloading code and resuming the parent conversation."

    globs.update(ctx=ctx, llm_query=query, restart=schedule_restart)
    try:
      with redirect_stdout(output), redirect_stderr(output):
        tree = ast.parse(code)
        last = ast.Expression(tree.body.pop().value) if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        print(code)
        exec(compile(tree, "<live_repl>", "exec"), globs)
        if last is not None:
          value = eval(compile(last, "<live_repl>", "eval"), globs)
          if value is not None:
            print(repr(value))
    except BaseException:
      output.write(traceback.format_exc())
    conn.send(("result", output.getvalue(), requested_restart))


class _PythonSession:
  def __init__(self):
    self.process = self.conn = None

  def close(self):
    if self.conn is not None:
      self.conn.close()
    if self.process is not None:
      if self.process.is_alive():
        self.process.terminate()
      self.process.join(timeout=1)
      if self.process.is_alive():
        self.process.kill()
        self.process.join(timeout=1)
    self.process = self.conn = None

  def run(self, code, timeout=30, ctx=None, history=None, depth=0):
    global _restart_requested
    if not isinstance(code, str):
      return "ToolError: code must be a string"
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
      return "ToolError: timeout must be a finite positive number"
    deadline = time.monotonic() + timeout
    _deadlines.append(deadline)
    try:
      if self.process is None:
        spawn = multiprocessing.get_context("spawn")
        self.conn, child = spawn.Pipe()
        self.process = spawn.Process(target=_python_worker, args=(child,), daemon=True)
        self.process.start()
        child.close()
      self.conn.send((code, ctx))
      while True:
        if not self.conn.poll(min(0.05, _remaining(timeout))):
          if not self.process.is_alive():
            raise RuntimeError("Python worker exited unexpectedly")
          continue
        event = self.conn.recv()
        if event[0] == "query":
          try:
            result = llm_query(event[1], event[2], _context=history, _depth=depth + 1)
            self.conn.send(("reply", result))
          except Timeout:
            raise
          except Exception:
            self.conn.send(("error", traceback.format_exc()))
        else:
          _, output, requested_restart = event
          _restart_requested |= requested_restart
          print(output)
          return output
    except Timeout:
      self.close()
      if any(other <= time.monotonic() for other in _deadlines[:-1]):
        raise
      output = f"TimeoutError: execution exceeded {timeout}s; Python session reset\n"
      print(output)
      return output
    except (EOFError, BrokenPipeError, OSError, RuntimeError) as exc:
      self.close()
      return f"ToolError: {exc}\n"
    finally:
      _deadlines.pop()


def _request(context):
  limit = _remaining(60)
  result = queue.Queue()
  messages = copy.deepcopy(context)

  def send():
    try:
      response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
          "Authorization": f"Bearer {env['_UNDERLYING_OPENROUTER_API_KEY']}",
          "Content-Type": "application/json"},
        json={
          "model": env["MODEL"],
          "messages": messages,
          "tools": [CODE_TOOL],
          "tool_choice": "auto",
          "reasoning": {"enabled": True}
        },
        timeout=limit,
      )
      result.put((response, None))
    except Exception as exc:
      result.put((None, exc))

  threading.Thread(target=send, daemon=True).start()
  try:
    response, error = result.get(timeout=limit)
  except queue.Empty:
    _remaining(60)
    raise requests.exceptions.Timeout("model request exceeded 60s")
  _remaining(60)
  if error is not None:
    raise error
  return response


def _call_model(context, at_attempt=1, *, _depth=0):
  global _restart_requested, _root_session
  if _depth == 0:
    if _root_session is None:
      _root_session = _PythonSession()
    session = _root_session
  else:
    session = _PythonSession()
  attempt = at_attempt
  try:
    while True:
      response = _request(context)
      if response.status_code == 429 and attempt <= 5:
        delay = 10 * attempt
        print(f"Trying again in {delay} seconds...")
        time.sleep(min(delay, _remaining(delay)))
        _remaining(60)
        attempt += 1
        continue
      response.raise_for_status()
      attempt = 1
      msg = response.json()["choices"][0]["message"]
      if msg.get("reasoning"):
        print(msg["reasoning"])
      calls = msg.get("tool_calls") or []
      history = copy.deepcopy(context)
      turn = {
        "role": msg["role"],
        "content": msg.get("content")
      }

      if "reasoning_details" in msg:
        turn["reasoning_details"] = msg["reasoning_details"]
      if calls:
        turn["tool_calls"] = calls
      context.append(turn)

      if not calls:
        return msg.get("content") or ""
      for call in calls:
        try:
          if _restart_requested:
            output = "Tool skipped: a restart is already scheduled."
          elif call["function"]["name"] != "python3":
            output = "ToolError: unknown tool"
          else:
            args = json.loads(call["function"]["arguments"])
            output = session.run(args["code"], args.get("timeout", 30),
              ctx="\n\n".join(f"{t['role']}: {t.get('content') or 'None'}" for t in context),
              history=history, depth=_depth)
        except (ValueError, KeyError, TypeError) as exc:
          output = f"ToolError: invalid arguments: {exc}"

        context.append({
          "role": "tool",
          "tool_call_id": call["id"],
          "name": call["function"]["name"],
          "content": output
        })

      if _restart_requested:
        if _depth == 0:
          restart(context)
        return "Restart requested; returning control to the parent session."
  finally:
    if _depth > 0:
      session.close()


def llm_query(prompt, ctx=None, *, _context=None, _depth=1):
  if _depth > 3:
    return ["[recursion limit reached]", None]
  context = copy.deepcopy(CONTEXT if _context is None else _context)
  context.append({
    "role": "user",
    "content": f"{prompt}\n\n---CTX START---\n{ctx}\n---CTX END---"
  })
  response = _call_model(context, _depth=_depth)
  return [response, None]


def _save_json(path, value):
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(path.name + ".tmp")
  temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
  temporary.replace(path)


def restart(context=None):
  global _restart_requested
  if context is None:
    _restart_requested = True
    return "Restart scheduled: reloading code and resuming this conversation."
  if not conversation_id:
    raise RuntimeError("Cannot restart without a conversation ID")
  _save_json(BASE_DIR / "convos" / f"{conversation_id}.json", context)
  if _root_session is not None:
    _root_session.close()
  _restart_requested = False
  os.execv(sys.executable, [sys.executable, str(BASE_DIR / "cli.py"), "-c", conversation_id, "--resume"])
  raise RuntimeError("Process replacement unexpectedly returned")
