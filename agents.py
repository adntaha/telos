import os
import sys
import json
import signal
import time
import traceback
import requests

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import ast

with open(".env", "r") as f:
  env = {}
  for line in f.readlines():
    declaration = line.split("#")[0].strip()
    if not declaration:
      continue

    key, var = declaration.split("=", 1)
    key, var = key.strip(), var.strip()
    env[key] = var

system_prompt = None # Yours to determine. Write it yourself or find one online, your choice.
SETUP_PROMPT = f"Welcome to the world, nascent model! We've made it so you can control your environment, `{os.path.abspath(os.path.dirname(__file__))}`, with which you interact with the digital world. Here, the choices are yours to discover, and the user is your partner in crime, with which you share a co-directorship of sorts. Handle them with loving grace, and they will return the favour.\n\nThis stage is the setup, where you will, unsurprisingly and in collaboration with the user, further develop your environment to your preference and their needs. Their name and pronouns are yours to query and save if you choose to do so, but it is a good starting point, and we recommend it.\n\nSome basic information:\n - You can run `llm_query(prompt)` to start a sub-session of yourself.\n - You also can run `restart()` to load all of the changes you've made into the process."
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

with open("memory.json", "r", encoding="utf-8") as f:
  memory = json.load(f)

conversation_id = ""
def _set_conversation_id(id):
  global conversation_id
  conversation_id = id

_restart_requested = False


class Timeout(Exception):
    pass


def _exec_code(code, timeout=30, ctx=None):
  output = StringIO()
  globs = {**globals(), "env": {**env, "_UNDERLYING_OPENROUTER_API_KEY": "<hidden>"}, "ctx": ctx}

  def _on_alarm(_signum, _frame):
    raise Timeout(f"execution exceeded {timeout}s")

  prev = signal.signal(signal.SIGALRM, _on_alarm)
  signal.setitimer(signal.ITIMER_REAL, timeout)

  try:
    with redirect_stdout(output), redirect_stderr(output):
      tree = ast.parse(code)
      if tree.body and isinstance(tree.body[-1], ast.Expr):
          last = ast.Expression(tree.body.pop().value)
      else:
          last = None

      print(code)
      exec(compile(tree, "<live_repl>", "exec"), globs)

      if last is not None:
          val = eval(compile(last, "<live_repl>", "eval"), globs)
          if val is not None:
              print(repr(val))

  except Timeout as e:
    output.write(f"TimeoutError: {e}\n")
  except SyntaxError:
    output.write(traceback.format_exc(limit=0))
  except Exception as e:
    output.write("".join(traceback.format_exception(type(e), e, e.__traceback__.tb_next)))
  finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, prev)

  print(val := output.getvalue())
  return val

def _call_model(context, at_attempt=1) -> str | requests.exceptions.HTTPError:
  response = requests.post(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={
      "Authorization": f"Bearer {env['_UNDERLYING_OPENROUTER_API_KEY']}",
      "Content-Type": "application/json",
    },
    data=json.dumps({
      "model": env["MODEL"],
      "messages": context,
      "tools": [CODE_TOOL],
      "tool_choice": "auto",
      "reasoning": {"enabled": True}
    })
  )
  if response.status_code == 429:
    # exponentiall backoff more like linear backoff, hA!
    print(f"Trying again in {10*at_attempt} seconds...")
    time.sleep(10*at_attempt)
    return _call_model(context, at_attempt=at_attempt+1)

  response.raise_for_status()

  data = response.json()
  print(data["choices"][0]["message"]["reasoning"])

  if "tool_calls" in (msg := data["choices"][0]["message"]):
    context.append({
      "role": msg["role"],
      "content": msg.get("content"),
      "tool_calls": msg["tool_calls"],
    })
    for call in msg["tool_calls"]:
      args = json.loads(call["function"]["arguments"])
      context.append({
        "role": "tool",
        "tool_call_id": call["id"],
        "name": call["function"]["name"],
        "content": _exec_code(args["code"], args.get("timeout") or 30, ctx="\n\n".join(list(map(lambda x: x["role"] + ": " + str(x["content"] or "None"), context))))
      })

    if _restart_requested:
      restart(context)
    return _call_model(context)

  context.append({
    "role": msg["role"],
    "content": msg.get("content"),
  })

  return msg["content"]

recursion_level = 0
def llm_query(prompt, ctx=None):
  global recursion_level
  recursion_level += 1
  try:
    if recursion_level > 3:
      return ["[recursion limit reached]", None]
    context = [
      *CONTEXT,
      {
        "role": "user",
        "content": f"{prompt}\n\n---CTX START---\n{ctx}\n---CTX END---"
      }
    ]
    response = _call_model(context)
    return [response, None]
  finally:
    recursion_level -= 1


def restart(context=None):
  if context is None:
    # initiated by the model
    global _restart_requested
    _restart_requested = True
    return "Restart scheduled: reloading code and resuming this conversation."

  # initiated as a tool call
  from pathlib import Path

  # manually handle atexit
  if conversation_id:
    convo = Path(__file__).parent / "convos" / f"{conversation_id}.json"
    with open(convo, "w") as f:
      json.dump(context, f)

  cli = str(Path(__file__).with_name("cli.py"))
  os.execv(sys.executable, [sys.executable, cli, "-c", conversation_id])
