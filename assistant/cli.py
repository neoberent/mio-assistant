"""`assistant` — tiny client for niri keybinds and scripts. Stdlib only, starts in ~30 ms.

  assistant ptt            push-to-talk (press again to stop early / interrupt her)
  assistant toggle         pin the character on screen / let it auto-hide
  assistant show | hide    force visibility
  assistant stop           stop speaking / listening
  assistant say TEXT       speak TEXT
  assistant ask TEXT       send a request as text (skips speech-to-text)
  assistant clip [SECS]     save the last SECS (default 30) of gameplay to the clips folder
  assistant forget         clear conversation memory
  assistant status         show state and today's Claude usage
  assistant escalations    what Mio couldn't do locally and sent to Claude (last 7 days)
  assistant learned        what Mio has learned: requests that worked, names she knows, word fixes
  assistant dream [--dry]  train her local model on her wins now (normally automatic while you're away)
"""
import json
import socket
import sys

from assistant.config import DAEMON_SOCK


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd, rest = args[0], " ".join(args[1:])
    req = {"cmd": cmd}
    if cmd in ("say", "ask"):
        req["text"] = rest
    if cmd == "dream":
        req["dry"] = rest == "--dry"
    if cmd == "clip" and rest.isdigit():
        req["seconds"] = int(rest)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(40)  # saving a long clip takes a moment
            s.connect(str(DAEMON_SOCK))
            s.sendall((json.dumps(req) + "\n").encode())
            resp = json.loads(s.makefile().readline() or "{}")
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("assistantd is not running (systemctl --user start assistantd)")
    if "error" in resp:
        sys.exit(resp["error"])
    if cmd == "status":
        print(json.dumps(resp, indent=1))
    elif cmd in ("clip", "dream"):
        print(resp.get("result", ""))
    elif cmd == "learned":
        print(f"{resp['skills']} learned requests ({resp['from_claude']} taught by Claude)")
        d = resp["last_dream"]
        print(f"Local model: {resp['model']}. Next dream: {resp['dream']}." + (
            f" Last dream {d['trained_at'][:16]}: exam {d['score']} vs {d['base_score']} for plain {d.get('base', 'qwen3:8b')}."
            if d.get("trained_at") else ""))
        print("\nRecent:")
        for line in resp["recent"]:
            print("  " + line[:150])
        print("\nNames she listens for: " + ", ".join(resp["names"]))
        if resp["fixes"]:
            print("Word fixes: " + ", ".join(f"{k} → {v}" for k, v in resp["fixes"].items()))
        if resp.get("words"):
            print(f"\nHer vocabulary: {resp['vocabulary']} words from what you said. Most used: "
                  + ", ".join(w for w, _ in resp["words"]))
    elif cmd == "escalations":
        print(resp.get("summary", ""))
        for r in resp.get("items", []):
            print(f"  {r['t'][5:16].replace('T', ' ')}  {r['text'][:70]:70}  ({r['reason'][:40]})")


if __name__ == "__main__":
    main()
