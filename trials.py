"""Each shop's own assistant, made in seconds: free trials and personal demos.

- A shop owner fills in /trial (name, kind of business, prices, hours, common questions) and gets a
  working assistant on its own page (/t/<slug>) plus one line of code to put it on their website.
- Every cold email links to a demo in the shop's own name (/t/<slug>), answering from the sample
  price list of its kind until the owner gives their real one.

Both are stored here; web.py serves them and bot.py builds each one's CustomerService on demand.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import sqlite3
from pathlib import Path

import demos as demos_mod

SLUG = re.compile(r"^[a-z0-9-]{3,40}$")
MAX_INFO = 6000

TRIAL_PERSONA = (
    "你是「{name}」的 AI 客服助理，替店家接待客人。{note}"
)
NOTE_TRIAL = "这是店主自己设置的免费试用版，资料是店主填的。"
NOTE_SAMPLE = ("下面的资料是这一类店的示范样本，还不是这家店真实的价目表。照资料回答就好；"
               "如果客人问价格是不是真的，就说这是示范，正式版会换成店家自己的资料。")


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-")
    return f"{base or 'shop'}-{secrets.token_hex(2)}"


class Trials:
    def __init__(self, db_path: Path | str):
        self.path = str(db_path)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trials (
                slug TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT, info TEXT, contact TEXT,
                source TEXT, lead_email TEXT, created_at TEXT)""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def create(self, name: str, kind: str, info: str = "", contact: str = "", source: str = "self",
               lead_email: str = "") -> dict:
        kind = kind if kind in demos_mod.DEMOS else demos_mod.DEFAULT_KIND
        row = {"slug": slugify(name), "name": name.strip()[:80], "kind": kind, "info": info.strip()[:MAX_INFO],
               "contact": contact.strip()[:120], "source": source, "lead_email": lead_email.lower(),
               "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        with self._db() as db:
            db.execute("INSERT INTO trials VALUES (:slug, :name, :kind, :info, :contact, :source, :lead_email, :created_at)", row)
        return row

    def get(self, slug: str) -> dict | None:
        if not SLUG.match(slug or ""):
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM trials WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None

    def for_lead(self, lead: dict, kind: str) -> dict:
        """The personal demo for a business we email: made once, the same one every time after."""
        email = lead["email"].lower()
        with self._db() as db:
            row = db.execute("SELECT * FROM trials WHERE lead_email=? AND source='lead'", (email,)).fetchone()
        return dict(row) if row else self.create(lead["name"], kind, source="lead", lead_email=email)

    def count(self, source: str) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM trials WHERE source=?", (source,)).fetchone()[0]


def catalog_for(row: dict) -> str:
    """What the shop's assistant answers from: the owner's own words, or its kind's sample list."""
    if row.get("info"):
        return f"shop: {row['name']}\n{row['info']}"
    sample = demos_mod.DEMOS.get(row.get("kind") or "", demos_mod.DEMOS[demos_mod.DEFAULT_KIND])["catalog"]
    lines = sample.split("\n")
    lines[0] = f"shop: {row['name']} (sample information for a demo; the real version uses the shop's own)"
    return "\n".join(line for line in lines if not line.startswith("location:"))


def persona_for(row: dict) -> str:
    return TRIAL_PERSONA.format(name=row["name"], note=NOTE_TRIAL if row.get("info") else NOTE_SAMPLE)


def public(row: dict) -> dict:
    """What the shop's page shows (never the contact or the email it was made for)."""
    d = demos_mod.DEMOS.get(row.get("kind") or "", demos_mod.DEMOS[demos_mod.DEFAULT_KIND])
    return {"slug": row["slug"], "name": row["name"], "kind": row.get("kind") or demos_mod.DEFAULT_KIND,
            "label": d["label"], "label_zh": d["label_zh"], "questions": d["questions"],
            "sample": not row.get("info"), "lead": row.get("source") == "lead"}
