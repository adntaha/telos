import os
import sys
import json
import readline
import argparse
import traceback
import uuid
import atexit

from agents import SETUP_PROMPT, _call_model, CONTEXT
from pathlib import Path

CONVERSATION_DIR = Path(".") / "convos"
CONVERSATION_DIR.mkdir(exist_ok=True)

with open("memory.json", "r") as f:
  memory = json.load(f)

def save_conversation(filepath: str):
  with open(filepath, "w") as f:
    json.dump(CONTEXT, f)

if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-c", "--conversation", type=str, default=None, help="conversation id to continue")
  args = parser.parse_args()

  conversation_id = args.conversation
  if conversation_id:
    hist_file = CONVERSATION_DIR / (conversation_id + ".json")
    if not hist_file.exists():
      print("Invalid conversation ID.")
      sys.exit(1)
    with open(hist_file, "r") as f:
      context = json.load(f)
      CONTEXT = context
      for turn in context:
        match turn["role"]:
          case "user":
            print("> " + turn["content"])
            readline.add_history(turn["content"])
          case "assistant":
            print(turn["content"])
          case "system":
            print(turn["content"])
  else:
    conversation_id = str(uuid.uuid4())
    hist_file = CONVERSATION_DIR / (conversation_id + ".json")
    hist_file.touch()
    with open(hist_file, "w") as f:
      f.write("[]")

  atexit.register(save_conversation, hist_file)

  if memory["_first_time_running"]:
    try:
      res = _call_model(CONTEXT := [*CONTEXT, {
        "role": "user",
        "content": SETUP_PROMPT
      }])
      print(res)
      memory["_first_time_running"] = False
      with open("memory.json", "w+") as f:
        json.dump(memory, f)
    except Exception:
      traceback.print_exc()

  while True:
    next_inp = input("> ").strip()

    if next_inp.lower() in ["quit", "exit"]:
      break

    try:
      res = _call_model(CONTEXT := [*CONTEXT, {
        "role": "user",
        "content": next_inp
      }])
      print(res)
    except Exception:
      traceback.print_exc()
