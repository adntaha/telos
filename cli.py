import json
try:
  import readline
except ImportError:
  readline = None
import argparse
import traceback
import uuid
import atexit

import agents

CONVERSATION_DIR = agents.BASE_DIR / "convos"
MEMORY_FILE = agents.BASE_DIR / "memory.json"


def save_conversation(filepath):
  agents._save_json(filepath, agents.CONTEXT)


def main(argv=None):
  parser = argparse.ArgumentParser()
  parser.add_argument("-c", "--conversation", help="conversation ID to continue")
  parser.add_argument("--resume", action="store_true", help="resume a pending model turn after restart")
  args = parser.parse_args(argv)
  if args.resume and not args.conversation:
    parser.error("--resume requires --conversation")
  if args.conversation and (not args.conversation.strip() or "/" in args.conversation or "\\" in args.conversation):
    parser.error("conversation ID must be a filename, not a path")
  CONVERSATION_DIR.mkdir(exist_ok=True)
  conversation_id = args.conversation or str(uuid.uuid4())
  hist_file = CONVERSATION_DIR / f"{conversation_id}.json"
  if args.conversation:
    if not hist_file.exists():
      parser.error("Invalid conversation ID")
    agents.CONTEXT[:] = json.loads(hist_file.read_text(encoding="utf-8"))
    if agents.system_prompt:
      if agents.CONTEXT and agents.CONTEXT[0]["role"] == "system":
        agents.CONTEXT[0]["content"] = agents.system_prompt
      else:
        agents.CONTEXT.insert(0, {"role": "system", "content": agents.system_prompt})
    for turn in agents.CONTEXT:
      content = turn.get("content") or ""
      match turn["role"]:
        case "user":
          print("> " + content)
          if readline:
            readline.add_history(content)
        case "assistant" | "system":
          print(content)
  else:
    save_conversation(hist_file)
  agents._set_conversation_id(conversation_id)
  atexit.register(save_conversation, hist_file)
  def close_python():
    if agents._root_session is not None:
      agents._root_session.close()
  atexit.register(close_python)
  memory = json.loads(MEMORY_FILE.read_text(encoding="utf-8")) if MEMORY_FILE.exists() else {"_first_time_running": True}

  def run_turn():
    try:
      print(agents._call_model(agents.CONTEXT))
      if memory.get("_first_time_running"):
        memory["_first_time_running"] = False
        agents._save_json(MEMORY_FILE, memory)
    except Exception:
      traceback.print_exc()
    finally:
      save_conversation(hist_file)

  if args.resume:
    run_turn()
  elif not args.conversation and memory.get("_first_time_running"):
    agents.CONTEXT.append({"role": "user", "content": agents.SETUP_PROMPT})
    run_turn()
  while True:
    try:
      text = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
      print()
      break
    if text.lower() in ("quit", "exit"):
      break
    if text:
      agents.CONTEXT.append({"role": "user", "content": text})
      run_turn()
  save_conversation(hist_file)
  close_python()


if __name__ == "__main__":
  main()
