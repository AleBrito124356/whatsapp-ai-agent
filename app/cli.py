"""Command-line tools: a WhatsApp chat simulator and quick database views.

    python -m app chat                      # talk to the real agent in your terminal
    python -m app chat --now 2026-09-24T09:00 --script examples/booking_es.txt
    python -m app bookings [--date 2026-09-25] [--status all]
    python -m app handoffs

``chat`` runs the real ``Agent`` in-process against a temporary SQLite
database (or ``--db``). Outbound messages are built by the same payload
builders used for the Graph API and printed instead of sent: buttons and list
rows are numbered, and typing a number taps that option. Nothing is sent to
Meta, whatever credentials your environment holds. The LLM is used only if
NVIDIA_API_KEY is set and ``--offline`` is not given.

Chat commands:
    <text>            send a text message        /say <text>   send text literally (e.g. "2")
    <number>          tap option N of the last buttons/list message
    /tap ID [TITLE]   send a raw interactive reply (try forging one)
    /image [CAPTION]  send a photo               /audio        send a voice note
    /staff TEXT       reply as staff (/admin rules: 24h window, opt-out)
    /resolve          staff hand the chat back to the bot
    /queue            open handoffs              /bookings     upcoming bookings
    /state            conversation state         /advance 25h  move the clock (m/h/d)
    /help             this help                  /quit         leave
"""

from __future__ import annotations

import argparse
import itertools
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TextIO

from .clock import UTC, parse_duration, parse_when
from .config import Settings, get_settings
from .models import InboundMessage
from .wa_payloads import Rendered, render

INDENT = "      "


class _OfflineLLM:
    available = False

    def chat(self, messages, temperature=0.3, max_tokens=600):
        return None


class SimClock:
    """Real time (or a frozen start) plus a manual offset for /advance."""

    def __init__(self, frozen: Optional[datetime] = None):
        self.frozen = frozen
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return (self.frozen or datetime.now(UTC)) + self.offset

    def advance(self, delta: timedelta) -> None:
        self.offset += delta


# ---------------------------------------------------------------- rendering
def format_rendered(view: Rendered, who: str = "bot") -> str:
    """Pretty-print one outbound message the way a phone would show it."""
    lines = view.body.splitlines() or [""]
    out = [f"{who} › {lines[0]}"] + [f"{INDENT}{line}" if line else "" for line in lines[1:]]
    if view.kind == "buttons":
        out.append(INDENT + "   ".join(f"[{i}] {o.title}" for i, o in enumerate(view.options, 1)))
    elif view.kind == "list":
        out.append(f"{INDENT}☰ {view.button_text}")
        for i, o in enumerate(view.options, 1):
            desc = f" · {o.description}" if o.description else ""
            out.append(f"{INDENT} {i:>2}) {o.title}{desc}")
    return "\n".join(out)


class ChatSimulator:
    def __init__(
        self,
        settings: Settings,
        db_path: Path,
        clock: SimClock,
        wa_id: str = "50760001234",
        profile_name: Optional[str] = "Ana",
        offline: bool = True,
        out: TextIO = sys.stdout,
    ):
        from .deps import build_services
        from .llm import LLMClient
        from .staff import StaffDesk
        from .wa_client import CallbackTransport, WhatsAppClient

        self.out = out
        self.clock = clock
        self.wa_id = wa_id
        self.profile_name = profile_name
        self.options: list = []
        self._ids = itertools.count(1)
        wa = WhatsAppClient(settings, transport=CallbackTransport(self._on_payload))
        llm = _OfflineLLM() if offline else LLMClient(settings)
        self.services = build_services(settings, db_path=db_path, clock=clock, llm=llm, wa=wa)
        self.desk = StaffDesk(self.services)
        self._staff_sending = False

    # ------------------------------------------------------------- output
    def say(self, line: str = "") -> None:
        print(line, file=self.out)

    def _on_payload(self, payload: dict) -> None:
        view = render(payload)
        if view.kind == "status":
            return  # read receipts / typing indicator
        if self._staff_sending:
            return  # already echoed by the /staff command
        self.say(format_rendered(view))
        if view.kind in ("buttons", "list"):
            self.options = view.options

    # -------------------------------------------------------------- input
    def _message(self, **fields) -> InboundMessage:
        return InboundMessage(
            wa_id=self.wa_id,
            message_id=f"wamid.SIM{next(self._ids)}",
            timestamp=str(int(self.clock().timestamp())),
            profile_name=self.profile_name,
            **fields,
        )

    def send_text(self, text: str) -> None:
        self.services.agent.handle_message(self._message(type="text", text=text))

    def tap(self, payload_id: str, title: str) -> None:
        self.services.agent.handle_message(
            self._message(type="interactive", interactive_id=payload_id, interactive_title=title)
        )

    def handle_line(self, line: str) -> bool:
        """Process one input line. Returns False to quit."""
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        if line.isdigit() and self.options:
            index = int(line)
            if 1 <= index <= len(self.options):
                option = self.options[index - 1]
                self.say(f"you › [{option.title}]")
                self.tap(option.id, option.title)
                return True
        if line.startswith("/"):
            return self._command(line)
        self.say(f"you › {line}")
        self.send_text(line)
        return True

    def _command(self, line: str) -> bool:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/help":
            self.say(__doc__.split("Chat commands:")[1].rstrip())
        elif cmd == "/say":
            self.say(f"you › {arg}")
            self.send_text(arg)
        elif cmd == "/tap":
            pid, _, title = arg.partition(" ")
            self.say(f"you › [tap {pid}]")
            self.tap(pid, title or pid)
        elif cmd == "/image":
            self.say(f"you › [photo] {arg}".rstrip())
            self.services.agent.handle_message(self._message(type="image", media_id="media.sim", caption=arg or None))
        elif cmd == "/audio":
            self.say("you › [voice note]")
            self.services.agent.handle_message(self._message(type="audio", media_id="media.sim"))
        elif cmd == "/advance":
            try:
                self.clock.advance(parse_duration(arg))
            except ValueError as exc:
                self.say(f"!! {exc}")
                return True
            self.say(f"   ⏱ clock advanced {arg}")
        elif cmd == "/staff":
            if self._staff(lambda: self.desk.reply(self.wa_id, arg), quiet=True) is not None:
                self.say(f"staff › {arg}")
        elif cmd == "/resolve":
            result = self._staff(lambda: self.desk.resolve(self.wa_id, notify=True), quiet=False)
            if result is not None:
                self.say(f"   ✔ handoff resolved ({result['resolved']} closed); the bot is back")
        elif cmd == "/queue":
            queue = self.desk.queue(last_messages=0)
            self.say(f"   {len(queue)} open handoff(s)")
            for item in queue:
                self.say(f"   - {item['wa_id']} {item['profile_name'] or ''} reason={item['reason']}")
        elif cmd == "/bookings":
            print_bookings(self.desk.bookings(status="all"), self.out)
        elif cmd == "/state":
            conv = self.services.store.get_conversation(self.wa_id)
            draft = {k: v for k, v in conv.draft.items() if not k.startswith("_")}
            self.say(
                f"   state={conv.state} lang={conv.lang} handoff={conv.handoff} "
                f"opted_out={conv.opted_out} draft={draft}"
            )
        else:
            self.say(f"!! unknown command {cmd} (try /help)")
        return True

    def _staff(self, action, quiet: bool):
        """Run a StaffDesk action; with quiet=True its WhatsApp send is not echoed."""
        from .staff import StaffError

        self._staff_sending = quiet
        try:
            return action()
        except StaffError as exc:
            self.say(f"   ✖ staff: {exc.code}: {exc.message}")
            return None
        finally:
            self._staff_sending = False


def print_bookings(rows: list[dict], out: TextIO) -> None:
    if not rows:
        print("   (no bookings)", file=out)
        return
    for b in rows:
        print(
            f"   #{b['id']:<4} {b['date']} {b['time']}  {b['status']:<9} {b['customer_name']:<20} "
            f"{b['service_label']:<18} {b['wa_id']}",
            file=out,
        )


# --------------------------------------------------------------------- main
def _existing_store(settings: Settings, db: Optional[str]):
    from .state import Store

    path = Path(db) if db else settings.db_path
    if not path.exists():
        raise SystemExit(f"No database at {path} (start the server once, or pass --db).")
    return Store(path, settings.schema_path)


def main(argv: Optional[list[str]] = None, *, settings: Optional[Settings] = None, stdin: TextIO = None, stdout: TextIO = None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m app", description="WhatsApp AI agent tools")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="chat with the agent in the terminal (nothing is sent to Meta)")
    chat.add_argument("--db", help="SQLite file to use (default: a temporary one)")
    chat.add_argument("--now", help="freeze the clock, e.g. 2026-09-24T09:00 (business timezone)")
    chat.add_argument("--script", help="replay inputs from a file instead of typing them")
    chat.add_argument("--from", dest="wa_id", default="50760001234", help="customer wa_id")
    chat.add_argument("--name", default="Ana", help="customer WhatsApp profile name")
    chat.add_argument("--offline", action="store_true", help="never call the LLM, even if a key is set")

    bookings = sub.add_parser("bookings", help="list bookings in a database")
    bookings.add_argument("--db")
    bookings.add_argument("--date", help="YYYY-MM-DD (default: all)")
    bookings.add_argument("--status", default="confirmed", choices=["confirmed", "cancelled", "conflict", "all"])

    handoffs = sub.add_parser("handoffs", help="list open handoffs in a database")
    handoffs.add_argument("--db")

    args = parser.parse_args(argv)
    settings = settings or get_settings()

    if args.command == "bookings":
        store = _existing_store(settings, args.db)
        rows = store.list_bookings(date=args.date, status=None if args.status == "all" else args.status)
        print_bookings(rows, stdout)
        return 0

    if args.command == "handoffs":
        store = _existing_store(settings, args.db)
        rows = store.open_handoffs()
        if not rows:
            print("   (no open handoffs)", file=stdout)
        for h in rows:
            print(f"   {h['created_at']}  {h['wa_id']:<14} {h['profile_name'] or '':<20} reason={h['reason']}", file=stdout)
        return 0

    frozen = parse_when(args.now, settings.timezone) if args.now else None
    tmp = None
    if args.db:
        db_path = Path(args.db)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="wa-agent-chat-")
        db_path = Path(tmp.name) / "chat.sqlite"
    try:
        sim = ChatSimulator(
            settings,
            db_path,
            SimClock(frozen),
            wa_id=args.wa_id,
            profile_name=args.name or None,
            offline=args.offline,
            out=stdout,
        )
        if args.script:
            for line in Path(args.script).read_text(encoding="utf-8").splitlines():
                if not sim.handle_line(line):
                    break
            return 0
        stdin = stdin or sys.stdin
        interactive = stdin.isatty()
        if interactive:
            print(f"{settings.business_name} — WhatsApp simulator. Type /help for commands, /quit to leave.", file=stdout)
        while True:
            if interactive:
                print("> ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if not line or not sim.handle_line(line):
                return 0
    finally:
        if tmp is not None:
            tmp.cleanup()
