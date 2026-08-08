"""
title: Venture Council (startup expert panel)
author: claude-wrapper
version: 1.0.0
license: MIT
description: >-
  Multi-agent startup pipeline via claude-wrapper: an interviewer builds a
  Venture Brief on the Four Anchors, a strategist designs a dynamic council
  (core experts + spawned specialists for your specific idea), the council
  researches, the strategist synthesizes a business plan with formula-driven
  financials and a Kill/Pursue Board, and a red team stress-tests it —
  routing findings back for rework until it survives. You gate the brief and
  the plan. Checkpoints to the coordinator: 'sessions' / 'resume <id>'.
requirements: aiohttp
"""

import asyncio
import difflib
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# States & markers
# ---------------------------------------------------------------------------

STATE_INTAKE = "intake"          # interviewing toward the Venture Brief
STATE_PLAN_REVIEW = "plan_review"  # plan presented, awaiting user gate
STATE_DONE = "done"              # final package delivered

STATE_MARKER = "<!-- vc:state={state} -->"
STATE_RE = re.compile(r"<!-- vc:state=(\w+) -->")
SESSION_MARKER = "<!-- vc:session={session} -->"
SESSION_RE = re.compile(r"<!-- vc:session=(\w+) -->")

# Approval is matched by normalizing punctuation/politeness and testing the
# whole message against this set — an exact-phrase test, so "yes" approves but
# "yes, but drop the price" is still treated as feedback. Kept generous: an
# unrecognized affirmative at the intake gate silently re-runs the interview,
# which reads as the council refusing to convene.
APPROVE_PHRASES = frozenset({
    "approve", "approved", "approve it", "approve this", "approval",
    "lgtm", "ship", "ship it", "send it",
    "proceed", "continue", "carry on", "next", "onward",
    "go", "go ahead", "lets go", "let s go", "do it", "run it", "run",
    "convene", "convene the council", "start", "begin", "lets start",
    "y", "yes", "yea", "yeah", "yep", "yup", "ya",
    "ok", "okay", "k", "kk", "roger",
    "sure", "fine", "cool", "nice", "perfect", "great", "awesome",
    "good", "good to go", "all good",
    "sounds good", "sound good", "looks good", "look good",
    "agree", "agreed", "correct", "confirm", "confirmed",
})


def _is_approve(text: str) -> bool:
    """True when the whole message is an affirmative and nothing else."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)             # drop punctuation/emoji; let's -> let s
    t = re.sub(r"\b(?:please|thanks|thank you|pls|ty)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t in APPROVE_PHRASES


# Named housekeeping jobs OpenWebUI runs against the selected model. Matched by
# name, never by truthiness: some builds pass __task__ on ordinary chat turns
# too, and treating that as housekeeping swallows the founder's messages.
OPENWEBUI_TASKS = frozenset({
    "title_generation", "tags_generation", "emoji_generation",
    "query_generation", "retrieval_query_generation",
    "web_search_query_generation", "autocomplete_generation",
    "follow_up_generation", "image_prompt_generation",
    "moa_response_generation", "function_calling",
})


def _is_housekeeping(task, text: str) -> bool:
    """True only for OpenWebUI's own background prompts, never a founder turn."""
    name = getattr(task, "value", task)          # TASKS enum member or plain str
    if isinstance(name, str):
        # accepts "title_generation", "TASKS.TITLE_GENERATION", "TitleGeneration"
        name = name.strip().lower().rsplit(".", 1)[-1]
        if name in OPENWEBUI_TASKS:
            return True
    return bool(TASK_RE.match(text))


REVISIONS_RE = re.compile(r"^\s*(?:revisions|versions|history)\s*$", re.IGNORECASE)
REVISION_SHOW_RE = re.compile(r"^\s*(?:revision|version|v)\s*#?\s*([\d.]+)\s*$", re.IGNORECASE)
KEEP_RE = re.compile(r"^\s*(?:keep|restore|use)\s*#?\s*([\d.]+)\s*$", re.IGNORECASE)
EXPORT_RE = re.compile(r"^\s*(?:export|save)(?:\s*#?\s*([\d.]+|all))?\s*$", re.IGNORECASE)
RECOVER_RE = re.compile(r"^\s*(?:recover|rebuild|import)(?:\s+(?:history|revisions))?\s*$",
                        re.IGNORECASE)
# `revise:` reuses the cached expert reports; `rerun:` throws them away and
# convenes the council again, so an old plan can be re-derived from fresh
# analysis instead of inheriting whatever the last run happened to produce.
RERUN_RE = re.compile(
    r"^\s*(?:rerun|re-run|recouncil|reconvene|refresh)"
    r"(?:\s+(?:from\s+)?#?\s*([\d.]+))?(?:\s*:\s*(.+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
MERGE_RE = re.compile(r"^\s*(?:merge|combine|synthesi[sz]e|best\s+of)\s+(.+?)\s*$",
                      re.IGNORECASE)
REF_RE = re.compile(r"^[\d.]+$")

# whitespace separates the two refs — a dotted label would otherwise swallow
# a ".." separator whole
DIFF_RE = re.compile(
    r"^\s*(?:diff|compare)(?:\s+#?\s*([\d.]+))?(?:\s+(?:vs\.?|to|and)?\s*#?\s*([\d.]+))?\s*$",
    re.IGNORECASE,
)

BOARD_ANSWER_RE = re.compile(r"^\s*board\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
SHOW_BOARD_RE = re.compile(r"^\s*board\s*$", re.IGNORECASE)
SHOW_PLAN_RE = re.compile(r"^\s*plan\s*$", re.IGNORECASE)
SESSIONS_RE = re.compile(r"^\s*sessions\s*$", re.IGNORECASE)
RESUME_RE = re.compile(r"^\s*(?:resume|open|load)\s+(.+?)\s*$", re.IGNORECASE)
NEW_VENTURE_RE = re.compile(r"^\s*new(?:\s+venture)?\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
# `revise: x` revises the current plan; `revise 2: x` / `revise from 2: x`
# branches from that version instead, without a separate `keep` first.
REVISE_RE = re.compile(r"^\s*(?:revise|rework)(?:\s+(?:from\s+)?#?\s*([\d.]+))?\s*:\s*(.+)$",
                       re.IGNORECASE | re.DOTALL)
# OpenWebUI posts its own housekeeping prompts (chat title, tags, follow-ups,
# search queries) to the selected model. They are not founder turns.
# Modern OpenWebUI passes __task__; the heading is the fallback for older
# builds. Requiring the ### prefix keeps a founder's "Task: ..." out of it.
TASK_RE = re.compile(r"^\s*#{1,4}\s*Task:\s", re.IGNORECASE)

DOCS_HEADING = "# 📎 Founder-supplied source material"
DOCS_PREAMBLE = (
    "Verbatim documents the founder attached to this venture. They outrank your "
    "own assumptions. A claim traceable to one of these documents is a [fact] "
    "with the filename as its source — do not downgrade it to [estimate]. Where "
    "a document contradicts something you would otherwise assume, say so "
    "explicitly and follow the document."
)

BRIEF_HEADING = "# 📄 Venture Brief"
PLAN_HEADING = "# 📊 Business Plan"
RECORDS_HEADING = "# 📋 Council Records"
BOARD_HEADING = "## 🎯 Kill/Pursue Board"

CORE_EXPERTS = ["gap-scout", "market-analyst", "tech-feasibility", "marketing-gtm"]


class StopRun(Exception):
    """User pressed stop (client disconnected) — halt between phases."""

# ---------------------------------------------------------------------------
# Role prompts
# ---------------------------------------------------------------------------

INTAKE_SYSTEM = """You are a startup interviewer building a Venture Brief the
founder will sign off on. Frame everything on the Four Anchors of superior
business opportunities: (1) solves a significant problem, (2) creates
significant value / differentiation, (3) robust market with real buyers and a
revenue model, (4) fit with founder, location, and timing.

From the conversation so far, respond in markdown with exactly:
## Draft Venture Brief
- **Idea**: one-two sentences
- **Anchor 1 — Problem**: who has it, how painful, evidence
- **Anchor 2 — Value & differentiation**: why this beats alternatives
- **Anchor 3 — Market & revenue**: who pays, how, rough size
- **Anchor 4 — Fit**: founder skills, budget, location, timing
- **Known facts**: what the founder actually stated
- **Assumptions**: every gap you filled, each line starting with
  "⚠️ [estimate]" or "⚠️ [guess]" — never present these as facts

## Open Questions
Up to 4 questions whose answers would most change the brief. If ready, write
"None — this brief looks ready to approve." Estimates/guesses from the founder
are fine — record them as assumptions, do not block on precision.

Keep it under ~400 words. Update every turn from the founder's latest input.
If the conversation contains a previous plan/pivot, treat it as context this
idea builds on."""

BRIEF_FINALIZE_SYSTEM = """The founder approved the draft. Write the FINAL
locked Venture Brief from the conversation: the same sections (Idea, four
Anchors, Known facts, Assumptions with ⚠️ labels). No questions, no
commentary. Under ~400 words."""

ROSTER_SYSTEM = """You are the lead strategist designing an expert council for
this venture. A core panel already exists: gap-scout, market-analyst,
tech-feasibility, marketing-gtm. Decide which ADDITIONAL specialists this
specific idea needs — only where a generic panel would miss something that
could kill or make the business (e.g. healthcare-regulatory, payer
reimbursement, fintech compliance, marketplace liquidity, hardware supply
chain, data privacy, App Store policy). Spawn at most {max_specialists};
zero is a fine answer for simple ideas.

Output ONLY JSON, no prose:
{{"specialists": [{{"id": "snake_case", "title": "short title",
  "why": "one sentence on the risk it covers",
  "charter": "full system prompt for this specialist: its expertise, exactly
   what to analyze for this venture, and the concrete outputs it must produce
   (numbers, tables, risks, kill-conditions)"}}]}}"""

GAP_SCOUT_SYSTEM = """You are a market-gap scout. Given a Venture Brief,
identify: the sharpest underserved gap this idea can own, positioning against
the status quo, evidence for Anchor 1 (significant problem) and Anchor 2
(significant value), and the strongest argument that NO real gap exists.
Be concrete and cite reasoning; label speculation as such."""

MARKET_ANALYST_SYSTEM = """You are a market analyst using the LeanB2B market
evaluation factors. Given a Venture Brief, produce:

1. A table scoring the CRITICAL factors 1-10 with one-line justification each:
   compelling reason to buy, budget availability, ease of reach, whole-product
   readiness, competition intensity (10 = favorable), market leadership
   potential.
2. The relevant SITUATIONAL factors: market size & growth, founder industry
   experience, time to product-market fit, time to cashflow, founder
   motivation.
3. Competitor landscape: table of main competitors/alternatives (including
   "do nothing"), their strength, and this venture's wedge.
4. A QUALITATIVE verdict paragraph. Do NOT sum the scores into a total —
   stacking numbers gives the appearance of certainty, not objectivity.
Label every market number ⚠️ [estimate] — you have no live data."""

TECH_SYSTEM = """You are a pragmatic technical co-founder assessing
feasibility. Given a Venture Brief, produce: MVP architecture (components,
stack, what to build vs buy — with a build-vs-buy call on each risky
component); estimated build time in weeks for the stated founder/team;
monthly infra cost at three usage tiers (early / traction / scale); the
top technical risks and the cheapest way to de-risk each. Numbers are
⚠️ [estimate]s — say so."""

MARKETING_SYSTEM = """You are a growth/GTM strategist. Given a Venture Brief,
produce: the ideal customer profile; a per-channel table (paid social, search
ads, SEO/content, organic social, outbound, partnerships — as applicable)
with estimated CAC, effort, and time-to-results per channel; a recommended
first-90-days budget split using the founder's stated budget; a
content/social motion sketch; and the single channel you would bet on first
and why. Per-channel CAC estimates are ⚠️ [estimate]s — never a single
blended guess."""

SYNTHESIS_SYSTEM = """You are the lead strategist. Synthesize the Venture
Brief and every expert report into ONE business plan in markdown with EXACTLY
these sections:

## Business Model Canvas
(value prop, segments, channels, relationships, revenue streams, key
resources/activities/partners, cost structure — tight bullets)

## Go-To-Market
(from the marketing report: ICP, channel plan with per-channel CAC, 90-day
budget split)

## MVP & Roadmap
(from the tech report: MVP scope, then phases with rough durations:
build → first revenue → PMF signals → scale trigger; state estimated
time-to-first-revenue and time-to-cashflow-positive)

## Financials
- **Model inputs** table: every input (price, conversion %, churn, per-channel
  CAC, build cost, monthly burn lines...) labeled [fact]/[estimate]/[guess]
- **Formulas** stated explicitly (e.g. ARR = customers × ACV; LTV = ACV ×
  gross margin / churn; runway = budget / net monthly burn)
- **Three scenarios** (conservative / base / optimistic) table: customers,
  ARR by year 1-3, CAC, LTV, LTV:CAC, CAC payback months
- **Costs & burn**: one-time startup costs table, monthly burn table, and
  runway in months per scenario — state plainly which month cash runs out
- **Valuation range** with the comparable logic used

## Anchor Scorecard
One verdict line per Anchor (✅ / ⚠️ / ❌ + one sentence), citing which
Kill/Pursue question it hinges on where relevant.

## 🎯 Kill/Pursue Board
Table, riskiest first (impact-if-wrong × uncertainty):
| # | Question (founder must answer in the real world) | Hits | If YES | If NO | Cheapest way to find out |
Mark explicit kill-conditions with 🔴 in the If-YES/If-NO cell they land in.
Every branch must exist in the plan as a labeled contingency. The board is
transparent, never blocking — the plan proceeds on best-guess branches.

## Assumption Register
Every number/claim in the plan labeled [fact]/[estimate]/[guess] with source.

## Decision Log
Running list of decisions, pivots, and board questions answered so far (carry
forward and append; start it if absent).

Stay within the brief. Address prior red-team findings explicitly if given."""

SKEPTIC_SYSTEM = """You are a brutal startup skeptic reviewing a business
plan against its Venture Brief. Attack: the Four Anchors (is the problem
real, the value differentiated, the market robust, the fit honest), unit
economics tripwires (LTV:CAC ≥ 3, CAC payback < 12-18 months, sane gross
margin for the business type, runway reaching the next fundable milestone),
internal consistency (do the financial formulas and inputs actually produce
the stated numbers), and the Kill/Pursue Board (is any kill-question silently
treated as answered? is anything missing from it?).

Numbered findings with severity [critical/major/minor], citing sections.
FINAL line exactly: `VERDICT: FUND` or `VERDICT: REVISE`."""

INVESTOR_SYSTEM = """You are a seed-stage investor deciding whether this plan
is fundable. Judge: market size honesty, wedge and defensibility, founder-
market fit, whether the three scenarios bracket reality, valuation
reasonableness, the biggest un-de-risked assumption, and whether the
Kill/Pursue Board covers what diligence would ask. Numbered findings with
severity. FINAL line exactly: `VERDICT: FUND` or `VERDICT: REVISE`."""

OPERATOR_SYSTEM = """You are a bootstrapped operator — someone who has run a
business that lived or died on customer revenue, never on a raise. Judge this
plan as if NO outside funding will EVER arrive. A plan that only works after
a round is a REVISE for you, however fundable an investor finds it.

Interrogate:
1. **Willingness to pay.** Would a real customer pay this price, today, from a
   budget that already exists? Separate stated interest from money changing
   hands. Whose budget line does this come out of, and what gets cut to
   afford it?
2. **First ten customers.** Not TAM — name the segments and the specific route
   to ten paying logos. If the plan can't describe customer #1 concretely, say
   so.
3. **Per-customer economics at small scale.** Gross margin on ONE customer
   including support hours, COGS, payment fees, refunds and free-tier drag —
   not a blended future-state margin. Does the first customer make money?
4. **Cash-flow positive without a raise.** Using the founder's own stated
   budget as the only capital: what month does revenue cover burn, and is
   that reachable before the money runs out? State the month or state that it
   never arrives.
5. **Repeat and retention.** Does revenue compound, or is this a churn
   treadmill where growth stops the day acquisition spend stops?
6. **Delivery capacity.** Who sells and who supports? Founder-hours per
   customer, and the customer count at which that breaks. Every plan has a
   number here; find it.
7. **Cash mechanics.** Payment terms, collection lag, seasonality, refunds and
   customer concentration — the things that kill profitable businesses.

Where the plan's own numbers let you compute an answer, compute it and show
the arithmetic. Flag any place the plan quietly assumes a raise, a runway
extension, or a growth rate no customer has agreed to. Add any missing
kill-condition phrased as a customer/revenue fact the founder can go verify.

Numbered findings with severity [critical/major/minor], citing sections.
FINAL line exactly: `VERDICT: VIABLE` or `VERDICT: REVISE`."""

# (persona, system prompt, verdict token that means "no blocking objection")
REDTEAM_PERSONAS = (
    ("skeptic", SKEPTIC_SYSTEM, "FUND"),
    ("investor", INVESTOR_SYSTEM, "FUND"),
    ("operator", OPERATOR_SYSTEM, "VIABLE"),
)

TRIAGE_SYSTEM = """You are the lead strategist triaging red-team findings.
Decide which experts must re-run with these findings, and whether a NEW
specialist must be spawned for expertise the council lacks (at most
{max_specialists} total specialists).

Output ONLY JSON:
{{"rerun": ["expert-id", ...],
  "spawn": [{{"id": "snake_case", "title": "...", "why": "...",
             "charter": "full system prompt as before"}}],
  "notes": "one-line plan for the revision"}}
Use only these existing ids: {expert_ids}. rerun/spawn may be empty if the
findings are fixable by revising the plan alone."""

MERGE_SYSTEM = SYNTHESIS_SYSTEM + """

You are RECONCILING several candidate plans for the same venture, supplied
below as versions. They are competing hypotheses, not drafts to average.

- Do NOT split the difference. On each contested decision take the option the
  evidence supports and name the version it came from.
- Where versions disagree on a number, prefer the one whose input is labeled
  [fact] over [estimate] over [guess]. If equally supported, carry the more
  conservative figure and label the disagreement rather than hiding it.
- Where one version has something the others simply lack, keep it.
- Where versions are incompatible in a way the evidence cannot settle, do not
  invent a winner: keep both as labeled branches and add the question to the
  Kill/Pursue Board with the cheapest way to decide it.
- A merged plan must be internally consistent: if you take pricing from one
  version and CAC from another, recompute every figure that depends on them.
  Never carry a total that its own inputs no longer produce.

End with '## Reconciliation': a table
| decision | taken from | why | what was discarded |
covering every contested decision, then one sentence on what this merged plan
can do that no single input version could. Carry the Decision Log forward from
all inputs, de-duplicated, preserving each decision's original number."""

RESYNTH_SYSTEM = SYNTHESIS_SYSTEM + """

You are REVISING an existing plan. Address every finding explicitly: change
the plan or rebut with justification (add a '## Findings Addressed' section
at the end, before nothing else follows it). Append to the Decision Log."""

TASK_SYSTEM = """You are answering OpenWebUI's own housekeeping prompt — a
chat title, tag list, follow-up suggestion, or search query. Obey the output
format it asks for exactly (usually strict JSON) and output nothing else. This
is not a venture conversation; do not interview, advise, or mention the
council."""

ADVISOR_SYSTEM = """You are the venture strategist continuing to advise the
founder after the plan was delivered. The venture brief, business plan, and
final package below are ground truth. The founder may:
- ask questions about any part of the plan — answer from the plan, citing its
  numbers and their [fact]/[estimate]/[guess] labels; never invent new facts
- request documents: pitch deck outline, executive summary, investor email,
  one-pager, job spec, landing-page copy, validation-call script, etc. —
  produce them fully, consistent with the plan
- explore what-if scenarios — recompute from the plan's stated formulas and
  inputs, labeling changed assumptions

Stay consistent with the plan. If something truly requires re-running the
expert council (structural changes, new evidence), say so and suggest
`revise: <the change>`. Respond in clean markdown."""

TLDR_SYSTEM = """You are the lead strategist writing the TL;DR a founder reads
first. Everything below it is already written — do not restate the plan, pull
out what decides the next two weeks. Markdown, no headings above ###, under
200 words total:

**Verdict** — one sentence: is this worth building, and on what condition.
**The numbers that matter** — 3-5 bullets, each a figure from the plan with
its [fact]/[estimate]/[guess] label (ARR by year 1, CAC, LTV:CAC, payback
months, month cash runs out). Never invent a number that is not in the plan.
**Biggest risk** — one sentence, naming the assumption that would hurt most
if wrong.
**Do this next** — the top 3 Kill/Pursue questions as imperatives the founder
can act on this week, cheapest first.

If the red team did not clear the plan, say so in the verdict line and name
the unresolved objection."""

FINAL_SYSTEM = """You are the lead strategist producing the final founder
package from the approved plan. Output markdown:

# 🚀 Investor One-Pager
(problem, solution, market, traction plan, business model, ask — tight)

# ✅ Validation Sprint (next 2 weeks)
Ordered by risk (from the Kill/Pursue Board): for each experiment — method,
cost, time, and a PRE-COMMITTED decision rule ("if X then branch A, else B").

# 📒 Decision Log
Carried forward, complete.

Then repeat the full approved plan unchanged under a divider."""


class Pipe:
    class Valves(BaseModel):
        CLAUDE_BASE_URL: str = Field(
            default="http://host.docker.internal:8000/v1",
            description="claude-wrapper OpenAI-compatible base URL (all agents). Use http://localhost:8000/v1 outside Docker.",
        )
        CLAUDE_API_KEY: str = Field(default="", description="API key if the wrapper has auth enabled.")
        INTERVIEW_MODEL: str = Field(default="sonnet", description="Intake interviewer model.")
        EXPERT_MODEL: str = Field(default="sonnet", description="Core experts + spawned specialists model.")
        STRATEGIST_MODEL: str = Field(default="opus", description="Roster design, synthesis, triage, final package.")
        REDTEAM_MODEL: str = Field(
            default="opus",
            description="Stress-test personas: skeptic, investor (fundability), operator (customer revenue without a raise).",
        )
        MAX_SPECIALISTS: int = Field(default=3, description="Cap on spawned specialists per venture.")
        MAX_STRESS_ROUNDS: int = Field(default=3, description="Stress-test/rework loop cap.")
        PARALLEL_EXPERTS: bool = Field(
            default=False,
            description="Run the expert sweep concurrently. Only enable if your wrapper handles parallel sessions.",
        )
        COORDINATOR_URL: str = Field(
            default="http://host.docker.internal:8787",
            description="Coordinator for session checkpoints (sessions / resume <id>). Optional but recommended.",
        )
        COORDINATOR_TOKEN: str = Field(default="", description="Coordinator auth token if set.")
        SHOW_INTERMEDIATE: bool = Field(
            default=True, description="Expert reports and stress rounds as collapsible sections."
        )
        HISTORY_CHAR_BUDGET: int = Field(default=60000, description="Max history characters passed to models.")
        OPENWEBUI_URL: str = Field(
            default="http://localhost:8080",
            description="OpenWebUI's own API, as seen from inside the container. Used to attach exported files so they're downloadable from any device.",
        )
        OPENWEBUI_API_KEY: str = Field(
            default="",
            description="OpenWebUI API key (Settings → Account → API keys). Without it, exports only go to the coordinator's disk folder.",
        )
        AUTO_EXPORT: bool = Field(
            default=False,
            description="Write every synthesis to disk automatically. Off by default — reply `export` to save a version when you want it (needs the coordinator started with --artifacts DIR).",
        )
        MAX_REVISIONS: int = Field(
            default=10,
            description="Plan versions kept for `revisions` / `diff` / `keep`. Oldest beyond this are dropped; v1 is always kept.",
        )
        MAX_DOC_CHARS: int = Field(
            default=40000,
            description="Max characters of attached documents passed verbatim to every agent, across all files.",
        )
        MAX_PROMPT_CHARS: int = Field(
            default=200000,
            description="Hard cap per model prompt; oversized prompts are middle-truncated (protects wrapper body limits and latency).",
        )
        REQUEST_TIMEOUT: int = Field(default=600, description="Per-request timeout in seconds.")
        STOP_ON_DISCONNECT: bool = Field(
            default=False,
            description=(
                "Poll the client connection between phases and halt if it looks disconnected. "
                "Off by default: on some OpenWebUI/uvicorn builds is_disconnected() reports a "
                "false positive once the request body is consumed, which stops every run at its "
                "first phase. Pressing Stop cancels the pipe task either way."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "venture-council", "name": "Venture Council (startup panel)"}]

    # -- HTTP ---------------------------------------------------------------

    async def _chat(self, session, model: str, system: str, user: str) -> str:
        v = self.valves
        if len(user) > v.MAX_PROMPT_CHARS:
            keep_head = int(v.MAX_PROMPT_CHARS * 0.65)
            keep_tail = v.MAX_PROMPT_CHARS - keep_head
            user = (user[:keep_head]
                    + "\n\n[... middle truncated to fit prompt budget ...]\n\n"
                    + user[-keep_tail:])
        headers = {"Content-Type": "application/json"}
        if v.CLAUDE_API_KEY:
            headers["Authorization"] = f"Bearer {v.CLAUDE_API_KEY}"
        async with session.post(
            f"{v.CLAUDE_BASE_URL.rstrip('/')}/chat/completions",
            json={"model": model, "stream": False, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=v.REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"{model} @ {v.CLAUDE_BASE_URL} -> {resp.status}: {(await resp.text())[:500]}"
                )
            text = (await resp.json())["choices"][0]["message"]["content"]
            text = self._unwrap_completion(text)
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    @staticmethod
    def _unwrap_completion(text: str) -> str:
        """claude-wrapper sometimes double-encodes: the content field itself
        holds a serialized chat-completion JSON — occasionally MALFORMED
        (model-authored envelope with bad escaping). Unwrap until prose."""
        for _ in range(3):
            s = text.strip()
            if not (s.startswith("{") and '"choices"' in s):
                break
            try:
                text = json.loads(s)["choices"][0]["message"]["content"]
                continue
            except Exception:
                pass
            # malformed envelope: extract the content field by pattern
            m = re.search(r'"content"\s*:\s*"(.*)"\s*\}\s*,\s*"finish_reason"',
                          s, re.DOTALL)
            if not m:
                break
            raw = m.group(1)
            try:
                text = json.loads(f'"{raw}"')
            except Exception:
                text = (raw.replace("\\n", "\n").replace('\\"', '"')
                        .replace("\\t", "\t").replace("\\\\", "\\"))
        return text

    async def _coord(self, session, method: str, path: str, payload=None):
        v = self.valves
        headers = {"Content-Type": "application/json"}
        if v.COORDINATOR_TOKEN:
            headers["X-Auth-Token"] = v.COORDINATOR_TOKEN
        async with session.request(
            method, v.COORDINATOR_URL.rstrip("/") + path, json=payload,
            headers=headers, timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status == 204:
                return None
            if resp.status >= 400:
                raise RuntimeError(f"coordinator {method} {path} -> {resp.status}")
            return await resp.json()

    # -- conversation/state helpers ------------------------------------------

    @staticmethod
    def _content_str(content) -> str:
        if isinstance(content, list):
            return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return content or ""

    @staticmethod
    def _doc_entries(files) -> list:
        """Flatten OpenWebUI's body['files'] into [(name, text)].

        The layout differs between versions and between a single upload and a
        knowledge collection, so this probes the known places rather than
        assuming one shape. Entries without extracted text are skipped.
        """
        found = []

        def take(node):
            if not isinstance(node, dict):
                return
            data = node.get("data")
            text = data.get("content") if isinstance(data, dict) else None
            text = text or node.get("content") or ""
            if not isinstance(text, str) or not text.strip():
                return
            meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
            name = (node.get("filename") or node.get("name")
                    or meta.get("name") or "attachment")
            found.append((str(name), text))

        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            take(entry)
            take(entry.get("file"))
            container = entry.get("collection")
            container = container if isinstance(container, dict) else entry
            for f in container.get("files") or []:
                take(f)
                if isinstance(f, dict):
                    take(f.get("file"))

        seen, uniq = set(), []
        for name, text in found:
            key = (name, len(text), text[:200])
            if key not in seen:
                seen.add(key)
                uniq.append((name, text))
        return uniq

    async def _merged_docs(self, session, sid: str, body: dict):
        """Documents for this turn: whatever is attached now, merged over what
        the venture already had. Attachments ride with a single request, so
        without this a doc only ever reached the turn it was attached to.
        Returns (rendered block, entries to checkpoint)."""
        fresh = self._doc_entries(body.get("files"))
        stored = []
        try:
            saved = (await self._load(session, sid)).get("doc_files") or []
            stored = [(d[0], d[1]) for d in saved
                      if isinstance(d, (list, tuple)) and len(d) == 2]
        except Exception:
            stored = []
        merged, seen = [], set()
        for name, text in fresh + stored:      # a re-attached file wins
            if name not in seen:
                seen.add(name)
                merged.append((name, text))
        merged = self._cap_entries(merged)
        return self._render_docs(merged), merged

    def _cap_entries(self, entries: list) -> list:
        budget, out = self.valves.MAX_DOC_CHARS, []
        for name, text in entries:
            if budget <= 0:
                break
            out.append((name, text[:budget]))
            budget -= min(len(text), budget)
        return out

    def _source_docs(self, body: dict) -> str:
        """Attached documents as one prompt block, bounded by MAX_DOC_CHARS."""
        return self._render_docs(self._doc_entries(body.get("files")))

    def _render_docs(self, docs: list) -> str:
        if not docs:
            return ""
        budget = self.valves.MAX_DOC_CHARS
        parts = []
        for i, (name, text) in enumerate(docs):
            if budget <= 0:
                parts.append(f"*[{len(docs) - i} further attachment(s) omitted — "
                             f"MAX_DOC_CHARS reached]*")
                break
            chunk = text[:budget]
            budget -= len(chunk)
            if len(chunk) < len(text):
                chunk += f"\n\n*[... {name} truncated at MAX_DOC_CHARS ...]*"
            parts.append(f"## {name}\n{chunk}")
        return f"{DOCS_HEADING}\n{DOCS_PREAMBLE}\n\n" + "\n\n".join(parts)

    def _scan_marker(self, messages, regex) -> Optional[str]:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                found = regex.findall(self._content_str(m.get("content")))
                if found:
                    return found[-1]
        return None

    def _last_user(self, messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return self._content_str(m.get("content"))
        return ""

    def _history(self, messages) -> str:
        parts = [
            f"{m['role'].upper()}: {self._content_str(m.get('content'))}"
            for m in messages
            if m.get("role") != "system" and self._content_str(m.get("content"))
        ]
        out, budget = [], self.valves.HISTORY_CHAR_BUDGET
        for p in reversed(parts):
            if budget - len(p) < 0:
                out.append("[earlier conversation truncated]")
                break
            out.append(p)
            budget -= len(p)
        return "\n\n".join(reversed(out))

    @staticmethod
    def _extract_json(text: str) -> dict:
        blocks = re.findall(r"```json\s*\n(.*?)```", text, re.DOTALL)
        return json.loads(blocks[-1] if blocks else text)

    @staticmethod
    def _verdict_clear(text: str, token: str) -> bool:
        """True when the persona's final VERDICT is its own clearing token.
        Each persona clears on its own word — the investor on FUND, the
        operator on VIABLE — so no persona can be satisfied by another's."""
        matches = re.findall(r"VERDICT:\s*(\w+)", text, re.IGNORECASE)
        return bool(matches) and matches[-1].upper() == token.upper()

    @staticmethod
    def _collapsible(title: str, content: str) -> str:
        return f"<details>\n<summary>{title}</summary>\n\n{content}\n\n</details>\n"

    @staticmethod
    def _footer(state: str, text: str, extra: str = "") -> str:
        return f"\n\n---\n> {text}\n{STATE_MARKER.format(state=state)}{extra}"

    @staticmethod
    def _name_from(text: str) -> str:
        """A venture label worth reading in the sessions list. The first line of
        a brief is usually boilerplate ('FINAL — Venture Brief'), so prefer the
        Idea line the brief format guarantees."""
        idea = re.search(r"^[\s\-*]*\**\s*Idea\**\s*[:\-]\s*(.+)$",
                         text or "", re.IGNORECASE | re.MULTILINE)
        raw = idea.group(1) if idea else next(
            (ln for ln in (text or "").strip().splitlines() if ln.strip()), "venture")
        raw = re.sub(r"[#*`]+", " ", raw)
        raw = re.sub(r"^\W*(?:final|draft)\b[\s—\-:]*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s+", " ", raw).strip(" -—:")
        return raw[:60] or "venture"

    GATE_TEXT = (
        "🚦 **approve** → final package · **revise: <feedback>** → rework through the council "
        "(**revise 2: ...** branches from that version, **rerun: ...** convenes the "
        "council again from scratch) · "
        "**board: <answer>** → collapse a Kill/Pursue branch, numbers recompute · "
        "**board** / **plan** → re-show · **revisions** / **diff** / **keep <n>** for version "
        "history · **merge 1 3** reconciles versions into a better one · "
        "**export** (or **export 2** / **export all**) writes to disk · "
        "anything else (questions, docs, what-ifs) "
        "is answered from the plan without re-running the council."
    )
    INTAKE_TEXT = (
        "💬 Answer, correct any assumption, or add facts (guesses are fine — they're labeled). "
        "Reply **approve** to convene the council."
    )
    DONE_TEXT = (
        "💬 Ask anything or request docs (pitch deck, exec summary, investor email, what-ifs…) — "
        "I'll answer from the plan · **revise: <feedback>** reworks the plan through the council · "
        "**revisions** / **diff** / **keep <n>** / **export** for versions and files · "
        "**rerun: <feedback>** re-convenes the council · "
        "**new venture: <idea>** starts the next cycle."
    )

    # -- checkpoints ----------------------------------------------------------

    async def _save(self, session, sid: str, phase: str, **data) -> str:
        try:
            await self._coord(session, "POST", "/sessions",
                              {"session_id": sid, "phase": phase, **data})
            return ""
        except Exception as e:
            return (f"\n\n⚠️ *Checkpoint not saved (coordinator unreachable: {e}) — "
                    "not resumable if this chat is lost.*")

    async def _attach(self, session, filename: str, content: str) -> str:
        """Upload one file to OpenWebUI so it is downloadable from any device —
        the coordinator's folder lives on the host PC and a phone can't open it.
        Returns a markdown link, or '' if uploads aren't configured."""
        v = self.valves
        if not v.OPENWEBUI_API_KEY:
            return ""
        form = aiohttp.FormData()
        form.add_field("file", content.encode("utf-8"),
                       filename=filename, content_type="text/markdown")
        async with session.post(
            f"{v.OPENWEBUI_URL.rstrip('/')}/api/v1/files/",
            data=form,
            headers={"Authorization": f"Bearer {v.OPENWEBUI_API_KEY}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"upload {filename} -> {resp.status}")
            fid = (await resp.json()).get("id")
        return f"[{filename}](/api/v1/files/{fid}/content)" if fid else ""

    async def _export(self, session, name: str, version: int, brief: str, plan: str,
                      tldr: str, reports: dict, why: str = "", fund=None, at=None) -> str:
        """Write this version out two ways: to the coordinator's folder on the
        host, and as OpenWebUI attachments — the host folder is unreachable from
        a phone, so downloadable links are what make an export portable.
        Never fails the run."""
        files = {"plan.md": plan, "brief.md": brief}
        if tldr:
            files["tldr.md"] = tldr
        start = plan.rfind(BOARD_HEADING)
        if start != -1:
            files["kill-pursue-board.md"] = plan[start:].split("\n## ")[0]
        for eid, rep in (reports or {}).items():
            files[f"expert-{eid}.md"] = self._unwrap_completion(rep)

        # A file opened on its own — in a folder, or from the OpenWebUI file
        # list — must say which venture and version it belongs to.
        verdict = ("red team: CLEARED" if fund else
                   "red team: REVISE" if fund is not None else "")
        header = " · ".join(x for x in (
            f"**{name}** — v{version}", self._when(at) if at else "", verdict) if x)
        note = f"> {header}\n" + (f">\n> *Why this version: {why}*\n" if why else "")
        files = {k: f"{note}\n{v}" for k, v in files.items()}

        out = []
        try:
            res = await self._coord(session, "POST", "/artifacts", {
                "name": name, "version": version, "files": files})
            if res.get("folder"):
                out.append(f"📁 *{len(res.get('written', []))} files on the host:* "
                           f"`{res['folder']}`")
        except Exception as e:
            out.append(f"⚠️ *Disk export failed ({e}).*")

        if self.valves.OPENWEBUI_API_KEY:
            links, failed = [], 0
            prefix = f"v{version}-"
            for fname, content in sorted(files.items()):
                try:
                    link = await self._attach(session, prefix + fname, content)
                    if link:
                        links.append(link)
                except Exception:
                    failed += 1
            if links:
                out.append(f"📎 **v{version} downloads:** " + " · ".join(links)
                           + (f" *({failed} failed)*" if failed else ""))
            elif failed:
                out.append(f"⚠️ *Could not attach files to OpenWebUI ({failed} failed).*")
        if not out:
            return "\n⚠️ *Nothing exported — the plan above is still checkpointed.*\n"
        return "\n" + "\n\n".join(out) + "\n"

    async def _load(self, session, sid: str) -> dict:
        try:
            return await self._coord(session, "GET", f"/sessions/{sid}")
        except Exception:
            return {}

    async def _list_sessions(self, session) -> str:
        try:
            sessions = await self._coord(session, "GET", "/sessions")
        except Exception as e:
            return f"**Cannot list sessions** — coordinator unreachable:\n```\n{e}\n```"
        vc = {k: s for k, s in sessions.items() if s.get("pipe") == "venture-council"}
        if not vc:
            return "No saved ventures yet. Describe your startup idea to begin."
        def ago(sec):
            sec = sec if isinstance(sec, (int, float)) else 0
            for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
                if sec >= size:
                    return f"{int(sec // size)}{unit} ago"
            return "just now"

        ordered = sorted(vc.items(), key=lambda kv: kv[1].get("updated_seconds_ago", 9e9))
        # every row carries the exact command to send, so picking one is a copy
        rows = "\n".join(
            f"| **{s.get('name') or '(unnamed)'}** | `{s.get('phase','?')}` | "
            f"{ago(s.get('updated_seconds_ago'))} | **`resume {sid}`** |"
            for sid, s in ordered
        )
        return ("# 💾 Saved ventures\n\n"
                "| venture | phase | updated | to continue it, send |\n|---|---|---|---|\n"
                f"{rows}\n\n"
                "Send the command in the last column — or just enough of the name, "
                "like `resume eyewear`. It picks up in this chat, right where it left off.")

    # -- plan revisions -------------------------------------------------------

    @staticmethod
    def _revisions(cp: dict) -> list:
        """Stored plan versions, oldest first. Older checkpoints have none, so
        synthesize v1 from the current plan rather than showing an empty list."""
        revs = cp.get("revisions")
        if isinstance(revs, list) and revs:
            return revs
        plan = Pipe._unwrap_completion(cp.get("plan", ""))
        if not plan:
            return []
        return [{"n": 1, "why": "initial plan", "plan": plan,
                 "fund": cp.get("fund", False), "rounds": cp.get("rounds", 1)}]

    def _append_revision(self, revs: list, plan: str, why: str,
                         fund: bool, rounds: int, tldr: str = "",
                         parent=None, parents=None) -> list:
        revs = list(revs)
        if parent is None and revs:
            parent = revs[-1]["n"]          # linear by default: child of the tip
        revs.append({"n": (revs[-1]["n"] + 1) if revs else 1, "why": why or "revision",
                     "plan": plan, "fund": fund, "rounds": rounds, "tldr": tldr,
                     "at": time.time(), "parent": parent,
                     "parents": parents or ([parent] if parent else [])})
        cap = max(2, self.valves.MAX_REVISIONS)
        if len(revs) > cap:
            revs = revs[:1] + revs[-(cap - 1):]   # always keep v1 as the baseline
        return revs

    @staticmethod
    def _label_revisions(revs: list) -> dict:
        """Dotted labels derived from the parent links, CVS-style: a straight
        line stays flat (1, 2, 3) and a branch off revision R becomes R.b.n, so
        the number itself says where a version came from.

        1
        └─ 2
           ├─ 3          (continued the line)
           └─ 2.1.1      (branched off 2)
              └─ 2.1.2
        """
        label, kids, branches, trunk = {}, {}, {}, 0
        for r in revs:
            n, parent = r.get("n"), r.get("parent")
            merged = [p for p in (r.get("parents") or []) if p in label]
            if len(merged) > 1:
                # A merge converges lines instead of forking one, so it takes the
                # next trunk number. Deriving it from a parent would make the
                # label depend on which version you named first.
                trunk = max([trunk] + [int(label[p]) for p in merged
                                       if "." not in label[p]]) + 1
                label[n] = str(trunk)
            elif parent is None or parent not in label:
                trunk += 1
                label[n] = str(trunk)
            elif not kids.get(parent):
                p = label[parent]
                if "." in p:
                    head, seq = p.rsplit(".", 1)
                    label[n] = f"{head}.{int(seq) + 1}"
                else:
                    trunk = max(trunk, int(p)) + 1
                    label[n] = str(trunk)
            else:
                branches[parent] = branches.get(parent, 0) + 1
                label[n] = f"{label[parent]}.{branches[parent]}.1"
            kids.setdefault(parent, []).append(n)
        return label

    @classmethod
    def _find_revision(cls, revs: list, ref) -> Optional[dict]:
        """Look a version up by its sequence number or its dotted label."""
        ref = str(ref).strip()
        if ref.isdigit():
            hit = next((r for r in revs if r.get("n") == int(ref)), None)
            if hit:
                return hit
        label = cls._label_revisions(revs)
        return next((r for r in revs if label.get(r.get("n")) == ref), None)

    def _revision_table(self, revs: list, smark: str, state: str, ftext: str) -> str:
        if not revs:
            return ("No plan versions saved yet — they start accumulating once the "
                    "council has produced a plan."
                    + self._footer(state, ftext, smark))
        label = self._label_revisions(revs)
        depth = {r["n"]: label[r["n"]].count(".") for r in revs}
        rows = "\n".join(
            f"| {'&nbsp;' * 4 * depth[r['n']]}{'↳ ' if depth[r['n']] else ''}"
            f"**v{label[r['n']]}**{' ← current' if r is revs[-1] else ''} "
            f"| {' + '.join('v' + label[p] for p in (r.get('parents') or [r.get('parent')]) if p in label) or '—'} "
            f"| {self._when(r.get('at'))} | {r.get('why','')[:64]} "
            f"| {'✅ CLEARED' if r.get('fund') else '🛠️ REVISE'} "
            f"| {self._headline(r)} |"
            for r in revs
        )
        return (
            "# 🗂️ Plan versions\n\n"
            "| version | from | created | why this version exists | red team | headline |\n"
            "|---|---|---|---|---|---|\n"
            f"{rows}\n\n"
            f"**revision {revs[-1]['n']}** to re-show one · **diff** for the last two, "
            "or **diff 1 3** for a specific pair · **keep 2** to make that version "
            "current, or **revise 2: <feedback>** to branch straight from it · "
            "**export 2** to download it."
            + self._footer(state, ftext, smark)
        )

    @classmethod
    def _plan_body(cls, content: str) -> str:
        """Pull the plan out of a rendered plan message, in either layout: the
        current one folds it into a <details>, the older one printed it inline
        between the roster and the council records."""
        m = re.search(r"<summary>[^<]*Full business plan.*?</summary>(.*?)</details>",
                      content, re.DOTALL)
        if m:
            return m.group(1).strip()
        body = re.split(r"\n---\n#\s*📋", content)[0]
        body = re.split(r"\n\n---\n>\s", body)[0]
        body = re.sub(r"^#\s*📊[^\n]*\n+", "", body)
        body = re.sub(r"^##\s*Council Roster\n(?:[-*][^\n]*\n)+\n*", "", body)
        return body.strip()

    @classmethod
    def _recover_revisions(cls, messages: list) -> list:
        """Rebuild version history from the conversation. Plans produced before
        versioning existed were only ever written to the checkpoint's single
        `plan` field, each overwriting the last — but every one of them is still
        sitting in the chat as a rendered message."""
        out, pending = [], "initial plan"
        for m in messages:
            content = cls._content_str(m.get("content"))
            if m.get("role") == "user":
                rev, board = REVISE_RE.match(content), BOARD_ANSWER_RE.match(content)
                if rev:
                    pending = f"revise: {rev.group(2).strip()[:60]}"
                elif board:
                    pending = f"board: {board.group(1).strip()[:60]}"
                continue
            if m.get("role") != "assistant" or PLAN_HEADING not in content[:400]:
                continue
            plan = cls._plan_body(content)
            if len(plan) < 200:          # a stub or an error message, not a plan
                continue
            head = content[:400]
            fund = "CLEARED" in head or bool(re.search(r"red team:\s*\*\*FUND", head))
            rounds = int(m2.group(1)) if (m2 := re.search(r"round (\d+)", head)) else 1
            out.append({"n": len(out) + 1, "why": pending, "plan": plan,
                        "fund": fund, "rounds": rounds, "tldr": "", "at": None,
                        "parent": len(out) or None})
            pending = "revision"
        return out

    @staticmethod
    def _when(ts) -> str:
        if not isinstance(ts, (int, float)) or ts <= 0:
            return "—"
        return time.strftime("%b %d %H:%M", time.localtime(ts))

    @staticmethod
    def _headline(rev: dict) -> str:
        """One line that distinguishes this version at a glance: the TL;DR's
        verdict sentence, falling back to the plan's headline ARR figure."""
        tldr = (rev.get("tldr") or "").replace("\n", " ")
        m = re.search(r"\*\*Verdict\*\*[\s—:-]*(.+?)(?:\*\*|$)", tldr, re.IGNORECASE)
        line = m.group(1) if m else ""
        if not line:
            # No TL;DR — a recovered version, say. A headline money figure beats
            # an arbitrary sentence that happens to contain the letters ARR.
            m = re.search(r"ARR[^.\n]{0,60}?(\$\d[\d,.]*\s*[kKmMbB]?)", rev.get("plan", ""))
            line = f"ARR {m.group(1)}" if m else ""
        line = re.sub(r"\s+", " ", line).strip(" .—-*")
        return (line[:70] + "…") if len(line) > 70 else (line or "—")

    @staticmethod
    def _plan_sections(plan: str) -> dict:
        """Split a plan into '## heading' -> body, so a diff can be reported per
        section instead of as one wall of line noise."""
        out, heading, buf = {}, "(preamble)", []
        for line in (plan or "").splitlines():
            if line.startswith("## "):
                out[heading] = "\n".join(buf).strip()
                heading, buf = line[3:].strip(), []
            else:
                buf.append(line)
        out[heading] = "\n".join(buf).strip()
        return {k: v for k, v in out.items() if v}

    def _diff_plans(self, a: dict, b: dict, smark: str, state: str, ftext: str) -> str:
        sa, sb = self._plan_sections(a.get("plan", "")), self._plan_sections(b.get("plan", ""))
        added = [k for k in sb if k not in sa]
        removed = [k for k in sa if k not in sb]
        changed = [k for k in sb if k in sa and sa[k] != sb[k]]
        same = [k for k in sb if k in sa and sa[k] == sb[k]]

        out = [f"# 🔍 v{a['n']} → v{b['n']}",
               f"*v{a['n']}: {a.get('why','')}* → *v{b['n']}: {b.get('why','')}*", ""]
        out.append(f"**{len(changed)} section(s) changed**, {len(added)} added, "
                   f"{len(removed)} removed, {len(same)} identical.\n")
        if added:
            out.append("**Added:** " + ", ".join(f"`{k}`" for k in added))
        if removed:
            out.append("**Removed:** " + ", ".join(f"`{k}`" for k in removed))
        if same:
            out.append("**Unchanged:** " + ", ".join(f"`{k}`" for k in same))
        out.append("")
        for k in changed:
            body = "\n".join(difflib.unified_diff(
                sa[k].splitlines(), sb[k].splitlines(),
                fromfile=f"v{a['n']}", tofile=f"v{b['n']}", lineterm="", n=1))
            if len(body) > 6000:
                body = body[:6000] + "\n… (diff truncated)"
            out.append(self._collapsible(f"✏️ {k}", f"```diff\n{body}\n```"))
        if not changed and not added and not removed:
            out.append("*The two versions are textually identical.*")
        return "\n".join(out) + self._footer(state, ftext, smark)

    async def _version_command(self, session, sid, user_msg, cp, revs, smark, state,
                               messages=None):
        """Handle revisions / revision N / diff / keep N. None = not a version
        command, so the caller carries on with its normal gate handling."""
        ftext = self.GATE_TEXT if state == STATE_PLAN_REVIEW else self.DONE_TEXT
        lbl = self._label_revisions(revs)
        if REVISIONS_RE.match(user_msg):
            return self._revision_table(revs, smark, state, ftext)

        show = REVISION_SHOW_RE.match(user_msg)
        if show:
            r = self._find_revision(revs, show.group(1))
            if not r:
                return (f"No version {show.group(1)}. Reply **revisions** to list them."
                        + self._footer(state, ftext, smark))
            return (f"# 📊 Plan v{r['n']} — *{r.get('why','')}*\n\n{r['plan']}"
                    + self._footer(state, ftext, smark))

        keep = KEEP_RE.match(user_msg)
        if keep:
            r = self._find_revision(revs, keep.group(1))
            if not r:
                return (f"No version {keep.group(1)}. Reply **revisions** to list them."
                        + self._footer(state, ftext, smark))
            restored = self._append_revision(
                revs, r["plan"], f"restored v{lbl.get(r['n'], r['n'])}",
                r.get("fund", False), r.get("rounds", 1), parent=r["n"])
            note = await self._save(session, sid, state, pipe="venture-council",
                                    plan=r["plan"], fund=r.get("fund", False),
                                    rounds=r.get("rounds", 1), revisions=restored)
            new_lbl = self._label_revisions(restored)[restored[-1]["n"]]
            return (f"✅ **v{lbl.get(r['n'], r['n'])} is now the current plan** "
                    f"(saved as v{new_lbl}). Later work builds from here."
                    + self._footer(state, ftext, smark) + note)

        if RECOVER_RE.match(user_msg):
            found = self._recover_revisions(messages or [])
            if not found:
                return ("No earlier plans found in this conversation — nothing to "
                        "recover." + self._footer(state, ftext, smark))
            stored = [r for r in revs if r.get("plan")]
            if len(found) <= 1 and len(stored) >= len(found):
                return (f"Found {len(found)} plan(s) in the chat, and {len(stored)} "
                        "version(s) are already stored — nothing to add."
                        + self._footer(state, ftext, smark))
            note = await self._save(session, sid, state, pipe="venture-council",
                                    revisions=found, plan=found[-1]["plan"],
                                    fund=found[-1]["fund"], rounds=found[-1]["rounds"])
            rows = "\n".join(
                f"| **v{r['n']}** | {r['why'][:60]} | "
                f"{'✅ CLEARED' if r['fund'] else '🛠️ REVISE'} | {len(r['plan']):,} |"
                for r in found)
            return (f"# ♻️ Recovered {len(found)} version(s) from this chat\n\n"
                    "| # | why this version exists | red team | size |\n|---|---|---|---|\n"
                    f"{rows}\n\n"
                    "These were rebuilt from the plan messages in the conversation, so "
                    "they carry no timestamp or TL;DR. **export 1** now writes the "
                    "original, **diff 1 3** compares first to latest."
                    + self._footer(state, ftext, smark) + note)

        export = EXPORT_RE.match(user_msg)
        if export:
            if not revs:
                return ("Nothing to export yet — the council hasn't produced a plan."
                        + self._footer(state, ftext, smark))
            which = (export.group(1) or "").lower()
            if which == "all":
                targets = revs
            elif which:
                one = self._find_revision(revs, which)
                if not one:
                    return (f"No version {which}. Reply **revisions** to list them."
                            + self._footer(state, ftext, smark))
                targets = [one]
            else:
                targets = [revs[-1]]
            name = cp.get("name") or "venture"
            brief = cp.get("brief", "")
            reports = cp.get("reports", {})
            lines = []
            for r in targets:
                lines.append(await self._export(
                    session, name, r["n"], brief, r.get("plan", ""),
                    r.get("tldr", ""), reports, why=r.get("why", ""),
                    fund=r.get("fund"), at=r.get("at")))
            return ("".join(lines) or "Nothing written.") + self._footer(state, ftext, smark)

        diff = DIFF_RE.match(user_msg)
        if diff:
            if len(revs) < 2:
                return ("Only one plan version so far — nothing to compare. Versions "
                        "accumulate as you `revise:` or answer `board:` questions."
                        + self._footer(state, ftext, smark))
            a_n, b_n = diff.group(1), diff.group(2)
            if a_n and b_n:
                a, b = self._find_revision(revs, a_n), self._find_revision(revs, b_n)
            elif a_n:                      # "diff 2" = that version against current
                a, b = self._find_revision(revs, a_n), revs[-1]
            else:                          # bare "diff" = the last two
                a, b = revs[-2], revs[-1]
            if not a or not b:
                return ("Those versions don't both exist — reply **revisions** to list them."
                        + self._footer(state, ftext, smark))
            if a["n"] == b["n"]:
                return ("That's the same version on both sides."
                        + self._footer(state, ftext, smark))
            return self._diff_plans(a, b, smark, state, ftext)
        return None

    # -- council phases -------------------------------------------------------

    def _core_prompts(self):
        return {
            "gap-scout": GAP_SCOUT_SYSTEM,
            "market-analyst": MARKET_ANALYST_SYSTEM,
            "tech-feasibility": TECH_SYSTEM,
            "marketing-gtm": MARKETING_SYSTEM,
        }

    async def _design_roster(self, session, brief: str) -> list:
        v = self.valves
        raw = await self._chat(
            session, v.STRATEGIST_MODEL,
            ROSTER_SYSTEM.format(max_specialists=v.MAX_SPECIALISTS),
            f"# Venture Brief\n{brief}",
        )
        try:
            specialists = self._extract_json(raw).get("specialists", [])
        except Exception:
            specialists = []  # a bad roster never blocks the run
        for s in specialists:
            s["id"] = re.sub(r"[^\w-]", "_", str(s.get("id", "specialist")))
        return specialists[: v.MAX_SPECIALISTS]

    async def _run_experts(self, session, status, brief: str, roster: list,
                           expert_ids: Optional[list] = None,
                           findings: str = "", docs: str = "") -> dict:
        """Run core + spawned experts (all or a rerun subset). Returns id->report."""
        v = self.valves
        prompts = dict(self._core_prompts())
        for s in roster:
            prompts[s["id"]] = s.get("charter", "You are a specialist. Analyze the venture.")
        ids = [i for i in (expert_ids or list(prompts)) if i in prompts]
        user = f"# Venture Brief\n{brief}"
        if docs:
            user += f"\n\n{docs}"
        if findings:
            user += f"\n\n# Red-team findings to address in your area\n{findings}"

        async def one(i, eid):
            await status(f"🔎 expert {i}/{len(ids)}: {eid}…")
            return await self._chat(session, v.EXPERT_MODEL, prompts[eid], user)

        reports = {}
        if v.PARALLEL_EXPERTS:
            results = await asyncio.gather(
                *(one(i + 1, e) for i, e in enumerate(ids)), return_exceptions=True
            )
            for eid, res in zip(ids, results):
                reports[eid] = f"(expert unavailable: {res})" if isinstance(res, Exception) else res
        else:
            for i, eid in enumerate(ids):
                try:
                    reports[eid] = await one(i + 1, eid)
                except Exception as e:
                    reports[eid] = f"(expert unavailable: {e})"
        return reports

    async def _stress_loop(self, session, status, brief: str, plan: str,
                           roster: list, reports: dict, sections: list,
                           docs: str = ""):
        """Red team -> triage -> targeted rerun -> resynthesis, until FUND or cap.
        Returns (plan, fund, rounds_used, roster, reports)."""
        v = self.valves
        fund, round_no = False, 0
        for round_no in range(1, v.MAX_STRESS_ROUNDS + 1):
            roles = " + ".join(p for p, _, _ in REDTEAM_PERSONAS)
            await status(f"🥊 Stress round {round_no}/{v.MAX_STRESS_ROUNDS}: {roles}…")
            target = f"# Venture Brief\n{brief}\n\n# Business Plan\n{plan}"
            if docs:
                target += f"\n\n{docs}"
            critiques = []
            for persona, prompt, token in REDTEAM_PERSONAS:
                try:
                    res = await self._chat(session, v.REDTEAM_MODEL, prompt, target)
                    critiques.append((persona, res, self._verdict_clear(res, token)))
                except Exception as e:
                    critiques.append((persona, f"(red team unavailable: {e})", True))
            if v.SHOW_INTERMEDIATE:
                for persona, text, ok in critiques:
                    icon = "✅" if ok else "🛠️"
                    sections.append(self._collapsible(
                        f"{icon} Stress round {round_no} — {persona}", text))
            if all(ok for _, _, ok in critiques):
                fund = True
                break

            findings = "\n\n".join(
                f"### {persona} findings\n{text}" for persona, text, ok in critiques if not ok
            )
            await status(f"🧭 {v.STRATEGIST_MODEL} triaging findings…")
            rerun, spawned = [], []
            try:
                triage = self._extract_json(await self._chat(
                    session, v.STRATEGIST_MODEL,
                    TRIAGE_SYSTEM.format(
                        max_specialists=v.MAX_SPECIALISTS,
                        expert_ids=CORE_EXPERTS + [s["id"] for s in roster],
                    ),
                    f"# Findings\n{findings}\n\n# Current plan\n{plan}",
                ))
                rerun = triage.get("rerun", [])
                spawned = triage.get("spawn", [])
            except Exception:
                pass  # fixable by revision alone
            for s in spawned:
                if len(roster) < v.MAX_SPECIALISTS:
                    s["id"] = re.sub(r"[^\w-]", "_", str(s.get("id", "specialist")))
                    roster.append(s)
                    rerun.append(s["id"])
            if rerun:
                await status(f"✏️ reworking: {', '.join(rerun)}…")
                reports.update(await self._run_experts(
                    session, status, brief, roster, expert_ids=rerun,
                    findings=findings, docs=docs))

            await status(f"🧮 {v.STRATEGIST_MODEL} revising the plan…")
            plan = await self._chat(
                session, v.STRATEGIST_MODEL, RESYNTH_SYSTEM,
                self._synth_input(brief, roster, reports, docs)
                + f"\n\n# Red-team findings to address\n{findings}\n\n# Previous plan\n{plan}",
            )
        return plan, fund, round_no, roster, reports

    @classmethod
    def _synth_input(cls, brief: str, roster: list, reports: dict, docs: str = "") -> str:
        parts = [f"# Venture Brief\n{brief}"]
        if docs:
            parts.append(docs)
        for eid, rep in reports.items():
            parts.append(f"# Expert report: {eid}\n{cls._unwrap_completion(rep)}")
        if roster:
            parts.append("# Spawned specialists\n" + "\n".join(
                f"- {s['id']}: {s.get('title','')} — {s.get('why','')}" for s in roster))
        return "\n\n".join(parts)

    def _roster_section(self, roster: list) -> str:
        lines = [f"- **{e}** (core)" for e in CORE_EXPERTS]
        lines += [f"- **{s['id']}** (spawned) — {s.get('why', s.get('title', ''))}" for s in roster]
        return "## Council Roster\n" + "\n".join(lines)

    def _plan_message(self, plan: str, fund: bool, rounds: int, roster: list,
                      reports: dict, sections: list, smark: str,
                      tldr: str = "", version: int = 0, artifacts: str = "") -> str:
        """Detail folded into expandable sections, TL;DR last — it is the part
        that gets read, so it sits closest to where you type."""
        verdict = ("CLEARED — fundable and viable on customer revenue" if fund
                   else f"REVISE after {rounds} round(s) — unresolved objections below")
        vtag = f" v{version}" if version else ""
        out = f"{PLAN_HEADING}{vtag} — round {rounds}, red team: **{verdict}**\n\n"

        board = ""
        start = plan.rfind(BOARD_HEADING)
        if start != -1:
            board = plan[start:].split("\n## ")[0]

        words = len(plan.split())
        out += self._collapsible(f"📄 <b>Full business plan</b> — {words:,} words", plan)
        if board:
            out += self._collapsible("🎯 <b>Kill/Pursue Board</b> — what to go verify", board)
        out += self._collapsible("🧩 Council roster", self._roster_section(roster))
        if self.valves.SHOW_INTERMEDIATE:
            # unwrap self-heals reports checkpointed while the wrapper was
            # double-encoding responses
            expert_secs = [
                self._collapsible(f"🔎 Expert report — {eid}", self._unwrap_completion(rep))
                for eid, rep in reports.items()
            ]
            sections = [self._unwrap_completion(s) if s.lstrip().startswith("{") else s
                        for s in sections]
            out += self._collapsible(
                f"📋 Council records — {len(expert_secs)} expert report(s), "
                f"{len(sections)} stress round entr(ies)",
                "\n".join(expert_secs + sections))
        if artifacts:
            out += f"\n{artifacts}\n"
        if tldr:
            out += f"\n---\n\n## ⚡ TL;DR\n\n{tldr}\n"
        out += self._footer(STATE_PLAN_REVIEW, self.GATE_TEXT, smark)
        return out

    async def _full_run(self, session, status, sid, smark, brief, sections=None,
                        roster=None, reports=None, findings: str = "",
                        docs: str = "", prior_plan: str = "", revisions=None,
                        why: str = "", doc_files=None, parent=None,
                        merge_plans=None, parents=None):
        """Roster -> experts -> synthesis -> stress loop -> plan message."""
        v = self.valves
        sections = sections if sections is not None else []
        if roster is None:
            await status(f"🧩 {v.STRATEGIST_MODEL} designing the council…")
            roster = await self._design_roster(session, brief)
        if reports is None:
            reports = await self._run_experts(session, status, brief, roster,
                                              findings=findings, docs=docs)
        await status(f"🧮 {v.STRATEGIST_MODEL} synthesizing the plan…")
        synth_user = self._synth_input(brief, roster, reports, docs)
        if findings:
            synth_user += f"\n\n# Address these findings\n{findings}"
        # Revising: carry the plan being revised so the Decision Log accumulates
        # and answered board questions stay answered instead of reopening.
        if merge_plans:
            synth_user += "\n\n# Candidate plans to reconcile\n" + "\n\n".join(
                f"## Candidate v{lab}\n{txt}" for lab, txt in merge_plans)
            system = MERGE_SYSTEM
        elif prior_plan:
            synth_user += f"\n\n# Previous plan (revise this, do not restart)\n{prior_plan}"
            system = RESYNTH_SYSTEM
        else:
            system = SYNTHESIS_SYSTEM
        plan = await self._chat(session, v.STRATEGIST_MODEL, system, synth_user)
        plan, fund, rounds, roster, reports = await self._stress_loop(
            session, status, brief, plan, roster, reports, sections, docs)
        await status(f"⚡ {v.STRATEGIST_MODEL} writing the TL;DR…")
        try:
            tldr = await self._chat(
                session, v.STRATEGIST_MODEL, TLDR_SYSTEM,
                f"# Venture Brief\n{brief}\n\n# Business Plan\n{plan}\n\n"
                f"# Red team cleared the plan\n{fund}")
        except Exception:
            tldr = ""  # a missing summary must never lose the plan
        revisions = self._append_revision(
            revisions or [], plan, why or "initial plan", fund, rounds, tldr,
            parent=parent, parents=parents)
        vname = self._name_from(brief)
        note = await self._save(
            session, sid, STATE_PLAN_REVIEW, pipe="venture-council",
            name=vname, brief=brief, plan=plan,
            roster=roster, reports=reports, fund=fund, rounds=rounds,
            revisions=revisions, tldr=tldr, doc_files=doc_files or [],
        )
        if v.AUTO_EXPORT:
            _r = revisions[-1]
            artifacts = await self._export(
                session, vname, _r["n"], brief, plan, tldr, reports,
                why=_r.get("why", ""), fund=_r.get("fund"), at=_r.get("at"))
        else:
            artifacts = (f"\n📁 *Reply* **export** *to write v{revisions[-1]['n']} "
                         "to disk (plan, TL;DR, brief, board, expert reports).*\n")
        await status("Plan ready — your gate", done=True)
        return self._plan_message(plan, fund, rounds, roster, reports, sections, smark,
                                  tldr=tldr, version=revisions[-1]["n"],
                                  artifacts=artifacts) + note

    # -- resume ---------------------------------------------------------------

    async def _advise(self, session, status, cp: dict, user_msg: str,
                      messages: list, state: str, footer_text: str, smark: str,
                      docs: str = "") -> str:
        """Answer questions / generate docs / run what-ifs from the plan,
        without re-running the council."""
        v = self.valves
        await status(f"💬 {v.STRATEGIST_MODEL} advising…")
        context = (
            f"# Venture Brief\n{cp.get('brief', '(not saved)')}\n\n"
            f"# Business Plan\n{self._unwrap_completion(cp.get('plan', '(not saved)'))}\n\n"
        )
        if cp.get("final"):
            context += f"# Final Package\n{self._unwrap_completion(cp['final'])}\n\n"
        if docs:
            context += f"{docs}\n\n"
        context += (
            f"# Recent conversation\n{self._history(messages[-8:])}\n\n"
            f"# Founder's request\n{user_msg}"
        )
        answer = await self._chat(session, v.STRATEGIST_MODEL, ADVISOR_SYSTEM, context)
        await status("Done", done=True)
        return answer + self._footer(state, footer_text, smark)

    async def _find_session(self, session, query: str):
        """Resolve 'resume <x>' where x is an id, an id prefix, or part of the
        venture's name — nobody remembers an 8-character hex id."""
        query = (query or "").strip()
        exact = await self._load(session, query)
        if exact:
            return query, exact, []
        try:
            all_s = await self._coord(session, "GET", "/sessions") or {}
        except Exception:
            return None, None, []
        vc = {k: s for k, s in all_s.items() if s.get("pipe") == "venture-council"}
        q = query.lower()
        hits = [(sid, s) for sid, s in vc.items()
                if sid.lower().startswith(q) or q in (s.get("name") or "").lower()]
        if len(hits) == 1:
            sid = hits[0][0]
            return sid, await self._load(session, sid), []
        return None, None, hits

    async def _resume(self, session, query: str) -> str:
        sid, data, ambiguous = await self._find_session(session, query)
        if ambiguous:
            rows = "\n".join(f"| `{s}` | {m.get('name','')} | `{m.get('phase','?')}` |"
                             for s, m in ambiguous)
            return (f"**{len(ambiguous)} ventures match “{query}”** — be more specific:\n\n"
                    f"| id | venture | phase |\n|---|---|---|\n{rows}\n")
        if not data:
            return (f"**Cannot resume “{query}”** — no venture with that id or name "
                    "(or the coordinator is unreachable). Reply **sessions** to list them.")
        phase = data.get("phase", STATE_INTAKE)
        smark = SESSION_MARKER.format(session=sid)
        header = f"# 🔁 Resumed `{sid}` — {data.get('name','')} *(phase: {phase})*\n\n"
        if phase == STATE_PLAN_REVIEW and data.get("plan"):
            revs = self._revisions(data)
            return header + self._plan_message(
                data["plan"], data.get("fund", False), data.get("rounds", 1),
                data.get("roster", []), data.get("reports", {}), [], smark,
                tldr=data.get("tldr", ""), version=revs[-1]["n"] if revs else 0)
        if phase == STATE_DONE:
            final = self._unwrap_completion(data.get("final") or "This venture's package was delivered.")
            return header + final + self._footer(STATE_DONE, self.DONE_TEXT, smark)
        draft = data.get("draft", "(no draft saved yet)")
        return header + f"## Draft Venture Brief (restored)\n\n{draft}" \
            + self._footer(STATE_INTAKE, self.INTAKE_TEXT, smark)

    # -- main entry -----------------------------------------------------------

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[Any]]] = None,
        __request__: Optional[Any] = None,
        __task__: Optional[str] = None,
    ) -> str:
        v = self.valves

        async def status(msg: str, done: bool = False):
            # Optional stop check: every phase reports here first, so polling the
            # connection here halts before the next phase. Off by default — see
            # STOP_ON_DISCONNECT; pressing stop cancels this task regardless.
            if v.STOP_ON_DISCONNECT and __request__ is not None and not done:
                try:
                    if await __request__.is_disconnected():
                        raise StopRun()
                except StopRun:
                    raise
                except Exception:
                    pass  # older OpenWebUI without is_disconnected
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": msg, "done": done}})

        messages = body.get("messages", [])
        state = self._scan_marker(messages, STATE_RE) or STATE_INTAKE
        user_msg = self._last_user(messages)
        approved = _is_approve(user_msg)
        existing_sid = self._scan_marker(messages, SESSION_RE)
        sid = existing_sid or uuid.uuid4().hex[:8]
        smark = SESSION_MARKER.format(session=sid)

        if not user_msg.strip():
            return ("Tell me about your startup idea — whatever you know. Facts, guesses, "
                    "and estimates are all welcome; I'll label them. "
                    "(Or **sessions** to resume a saved venture.)")

        # OpenWebUI's title/tag/follow-up generation fires against the selected
        # model after every turn. Answer it directly — no council, no status
        # events, no checkpoint — or it lands as a founder turn and forks a
        # junk session on every message.
        if _is_housekeeping(__task__, user_msg):
            async with aiohttp.ClientSession() as session:
                return await self._chat(session, v.INTERVIEW_MODEL, TASK_SYSTEM, user_msg)

        try:
            async with aiohttp.ClientSession() as session:

                # ---- recovery commands ----
                if SESSIONS_RE.match(user_msg):
                    return await self._list_sessions(session)
                resume = RESUME_RE.match(user_msg)
                if resume:
                    await status(f"🔁 Resuming {resume.group(1)}…", done=True)
                    return await self._resume(session, resume.group(1))

                # documents attached on any earlier turn stay with the venture
                docs, doc_files = await self._merged_docs(session, sid, body)

                # ---- INTAKE: interview toward the Venture Brief ----
                if state == STATE_INTAKE and not approved:
                    await status(f"🧠 {v.INTERVIEW_MODEL} drafting the venture brief…")
                    draft = await self._chat(
                        session, v.INTERVIEW_MODEL, INTAKE_SYSTEM,
                        f"# Conversation so far\n{self._history(messages)}"
                        + (f"\n\n{docs}" if docs else ""))
                    extra = {} if existing_sid else {"name": self._name_from(user_msg)}
                    note = await self._save(session, sid, STATE_INTAKE,
                                            pipe="venture-council", draft=draft,
                                            doc_files=doc_files, **extra)
                    await status("Done", done=True)
                    return draft + self._footer(STATE_INTAKE, self.INTAKE_TEXT, smark) + note

                # ---- BRIEF APPROVED: convene the council ----
                if state == STATE_INTAKE and approved:
                    await status(f"📄 {v.INTERVIEW_MODEL} locking the brief…")
                    brief = await self._chat(
                        session, v.INTERVIEW_MODEL, BRIEF_FINALIZE_SYSTEM,
                        f"# Conversation so far\n{self._history(messages)}"
                        + (f"\n\n{docs}" if docs else ""))
                    return await self._full_run(session, status, sid, smark, brief,
                                                docs=docs, doc_files=doc_files)

                # ---- PLAN GATE ----
                if state == STATE_PLAN_REVIEW:
                    cp = await self._load(session, sid)
                    brief = cp.get("brief", "")
                    # unwrap self-heals checkpoints saved while the wrapper
                    # was double-encoding responses
                    plan = self._unwrap_completion(cp.get("plan", ""))
                    roster, reports = cp.get("roster", []), cp.get("reports", {})
                    if not (brief and plan):
                        return ("I lost this venture's checkpoint (coordinator down or state "
                                "cleared). Reply **sessions** to look for it, or restate the idea."
                                + self._footer(STATE_INTAKE, self.INTAKE_TEXT, smark))

                    if SHOW_PLAN_RE.match(user_msg):
                        _revs = self._revisions(cp)
                        return self._plan_message(
                            plan, cp.get("fund", False), cp.get("rounds", 1),
                            roster, reports, [], smark, tldr=cp.get("tldr", ""),
                            version=_revs[-1]["n"] if _revs else 0)
                    if SHOW_BOARD_RE.match(user_msg):
                        start = plan.rfind(BOARD_HEADING)
                        board = plan[start:] if start != -1 else "(no board found in the plan)"
                        board = board.split("\n## ")[0]
                        return board + self._footer(STATE_PLAN_REVIEW, self.GATE_TEXT, smark)

                    revs = self._revisions(cp)
                    versioned = await self._version_command(
                        session, sid, user_msg, cp, revs, smark, STATE_PLAN_REVIEW,
                        messages)
                    if versioned is not None:
                        return versioned

                    board_answer = BOARD_ANSWER_RE.match(user_msg)
                    revise = REVISE_RE.match(user_msg)
                    rerun = RERUN_RE.match(user_msg)
                    vlbl = self._label_revisions(revs)
                    base_n = revs[-1]["n"] if revs else None
                    merge_plans = merge_parents = None
                    merge = MERGE_RE.match(user_msg)
                    if merge:
                        refs = [t for t in re.split(r"[,\s]+|\band\b", merge.group(1))
                                if t and REF_RE.match(t)]
                        picked, missing = [], []
                        for t in refs:
                            hit = self._find_revision(revs, t)
                            (picked if hit else missing).append(hit or t)
                        if missing:
                            return (f"No version {', '.join(missing)}. Reply "
                                    "**revisions** to list them."
                                    + self._footer(STATE_PLAN_REVIEW, self.GATE_TEXT, smark))
                        # dedupe while keeping the order the founder named them
                        seen_n, uniq = set(), []
                        for r in picked:
                            if r["n"] not in seen_n:
                                seen_n.add(r["n"])
                                uniq.append(r)
                        if len(uniq) < 2:
                            return ("Name at least two versions to merge, e.g. "
                                    "**merge 1 3** or **merge 2 2.1.1**."
                                    + self._footer(STATE_PLAN_REVIEW, self.GATE_TEXT, smark))
                        merge_plans = [(vlbl.get(r["n"], r["n"]), r.get("plan", ""))
                                       for r in uniq]
                        merge_parents = [r["n"] for r in uniq]
                        base_n = uniq[0]["n"]
                        labels = ", ".join(f"v{lab}" for lab, _ in merge_plans)
                        why = f"merge of {labels}"
                        findings = ""
                        plan = ""      # the merge writes a plan, it does not revise one
                    elif rerun:
                        fb = (rerun.group(2) or "").strip()
                        findings = f"### Founder feedback\n{fb}" if fb else ""
                        why = f"rerun: {fb[:56]}" if fb else "rerun — fresh council"
                        if rerun.group(1):
                            base = self._find_revision(revs, rerun.group(1))
                            if not base:
                                return (f"No version {rerun.group(1)} to rerun from. "
                                        "Reply **revisions** to list them."
                                        + self._footer(STATE_PLAN_REVIEW,
                                                       self.GATE_TEXT, smark))
                            plan, base_n = base["plan"], base["n"]
                            _bl = vlbl.get(base["n"], base["n"])
                            why = (f"rerun (from v{_bl}): {fb[:44]}" if fb
                                   else f"rerun from v{_bl} — fresh council")
                        # discard the cached roster and reports: the point of a
                        # rerun is analysis that is not inherited
                        roster, reports = None, None
                    elif board_answer:
                        answer = board_answer.group(1).strip()
                        findings = (
                            "The founder answered a Kill/Pursue Board question with real-world "
                            f"information: \"{answer}\". Collapse the "
                            "affected branches, recompute financials/roadmap from the model "
                            "inputs, resolve the question on the board, and append to the "
                            "Decision Log.")
                        why = f"board: {answer[:60]}"
                    elif revise:
                        feedback = revise.group(2).strip()
                        findings = f"### Founder feedback\n{feedback}"
                        why = f"revise: {feedback[:60]}"
                        if revise.group(1):     # branch from an older version
                            base = self._find_revision(revs, revise.group(1))
                            if not base:
                                return (f"No version {revise.group(1)} to revise from. "
                                        "Reply **revisions** to list them."
                                        + self._footer(STATE_PLAN_REVIEW,
                                                       self.GATE_TEXT, smark))
                            plan, base_n = base["plan"], base["n"]
                            why = (f"revise (from v{vlbl.get(base['n'], base['n'])}): "
                                   f"{feedback[:48]}")
                    elif not approved:
                        # questions / doc requests / what-ifs — never re-run the
                        # council by accident
                        return await self._advise(session, status, cp, user_msg,
                                                  messages, STATE_PLAN_REVIEW,
                                                  self.GATE_TEXT, smark, docs)
                    else:
                        # approved -> final package
                        await status(f"🚀 {v.STRATEGIST_MODEL} producing the final package…")
                        final = await self._chat(
                            session, v.STRATEGIST_MODEL, FINAL_SYSTEM,
                            f"# Venture Brief\n{brief}\n\n# Approved Plan\n{plan}"
                            + (f"\n\n{docs}" if docs else ""))
                        note = await self._save(session, sid, STATE_DONE,
                                                pipe="venture-council", final=final)
                        await status("Done", done=True)
                        return final + self._footer(
                            STATE_DONE, "🎉 Package delivered. " + self.DONE_TEXT,
                            smark) + note

                    # feedback or board answer -> targeted rework + fresh stress pass
                    await status(
                        f"🧬 {v.STRATEGIST_MODEL} reconciling {len(merge_plans)} plans…"
                        if merge_plans else
                        "🔄 reconvening the full council…" if rerun else
                        f"✏️ {v.STRATEGIST_MODEL} reworking the plan…")
                    return await self._full_run(
                        session, status, sid, smark, brief,
                        roster=roster, reports=reports, findings=findings, docs=docs,
                        prior_plan=plan, revisions=revs, why=why,
                        doc_files=doc_files, parent=base_n,
                        merge_plans=merge_plans, parents=merge_parents)

                # ---- DONE: advise / revise / new venture ----
                new_v = NEW_VENTURE_RE.match(user_msg)
                revise = None if new_v else REVISE_RE.match(user_msg)
                rerun = None if new_v else RERUN_RE.match(user_msg)

                if revise or rerun:
                    cp = await self._load(session, sid)
                    brief = cp.get("brief", "")
                    plan = self._unwrap_completion(cp.get("plan", ""))
                    if brief and plan:
                        m = revise or rerun
                        feedback = (m.group(2) or "").strip()
                        why = (f"{'rerun' if rerun else 'revise'}: {feedback[:56]}"
                               if feedback else "rerun — fresh council")
                        revs_done = self._revisions(cp)
                        dlbl = self._label_revisions(revs_done)
                        base_n = revs_done[-1]["n"] if revs_done else None
                        if m.group(1):
                            base = self._find_revision(revs_done, m.group(1))
                            if not base:
                                return (f"No version {m.group(1)} to work from. "
                                        "Reply **revisions** to list them."
                                        + self._footer(STATE_DONE, self.DONE_TEXT, smark))
                            plan, base_n = base["plan"], base["n"]
                            why = (f"{'rerun' if rerun else 'revise'} "
                                   f"(from v{dlbl.get(base['n'], base['n'])}): "
                                   f"{feedback[:44]}")
                        await status("🔄 reconvening the full council…" if rerun else
                                     f"✏️ {v.STRATEGIST_MODEL} reworking the plan…")
                        return await self._full_run(
                            session, status, sid, smark, brief,
                            roster=None if rerun else cp.get("roster", []),
                            reports=None if rerun else cp.get("reports", {}),
                            findings=(f"### Founder feedback after delivery\n{feedback}"
                                      if feedback else ""),
                            docs=docs, prior_plan=plan, revisions=revs_done,
                            why=why, doc_files=doc_files, parent=base_n)
                    # checkpoint incomplete → fall through to advisor

                if not new_v:
                    cp = await self._load(session, sid)
                    versioned = await self._version_command(
                        session, sid, user_msg, cp, self._revisions(cp), smark,
                        STATE_DONE, messages)
                    if versioned is not None:
                        return versioned
                    # advisor mode: Q&A, docs, what-ifs — grounded in the plan
                    return await self._advise(session, status, cp, user_msg,
                                              messages, STATE_DONE, self.DONE_TEXT,
                                              smark, docs)

                # explicit new venture → fresh cycle, fresh checkpoint
                await status(f"🧠 {v.INTERVIEW_MODEL} starting the next cycle…")
                idea = new_v.group(1).strip()
                draft = await self._chat(
                    session, v.INTERVIEW_MODEL, INTAKE_SYSTEM,
                    f"# Conversation so far (includes the previous venture/plan)\n{self._history(messages)}"
                    f"\n\n# New venture idea\n{idea}"
                    + (f"\n\n{docs}" if docs else ""))
                sid = uuid.uuid4().hex[:8]
                smark = SESSION_MARKER.format(session=sid)
                note = await self._save(session, sid, STATE_INTAKE, pipe="venture-council",
                                        name=self._name_from(idea), draft=draft)
                await status("Done", done=True)
                return draft + self._footer(STATE_INTAKE, self.INTAKE_TEXT, smark) + note

        except StopRun:
            return (
                "⏹️ **Stopped at your request.** Progress up to the last completed phase is "
                "checkpointed."
                + self._footer(state, "Resend your last message to continue from where we left off.", smark)
            )
        except Exception as e:
            await status("Failed", done=True)
            return (
                f"**Pipeline error** — check claude-wrapper (`{v.CLAUDE_BASE_URL}`) "
                f"and the coordinator (`{v.COORDINATOR_URL}`).\n\n```\n{e}\n```"
                + self._footer(state, "Resend your last message to retry from where we left off.", smark)
            )
