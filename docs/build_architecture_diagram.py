#!/usr/bin/env python3
"""
Render the architecture diagram for this sample.

Outputs `architecture.png` beside this script. Regenerate it after changing the
stack so the picture in the README cannot drift from the code.

    pip install matplotlib
    python3 docs/build_architecture_diagram.py

Layout is hand-placed, and long edges are routed through explicit waypoints, so
nothing crosses a box it has no relationship with. A graph layout engine was
tried first and would not keep the trust boundary legible.

The diagram has one job: show that both AI surfaces use OpenAI models but reach
them by different routes, and that only the claims-review route passes through
AWS where a Bedrock guardrail can screen it. The voice route is drawn leaving the
AWS boundary along the bottom corridor because that is a real trust boundary.

Service boxes use AWS category colours. The OpenAI endpoint is a neutral box, not
a vendor logo, so the diagram makes no implied claim about the provider's brand.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent

NETWORK, STORAGE, SECURITY = "#8C4FFF", "#569A31", "#DD344C"
COMPUTE, DATABASE, ML, MGMT = "#ED7100", "#3B48CC", "#01A88D", "#E7157B"
NEUTRAL = "#5A6470"
GREEN, ORANGE, BLUE, GREY = "#0F7B3A", "#D97400", "#1F4FD8", "#7A838F"
INK = "#1A2027"

ZONES = {"browser": ("#6E40C9", "#F7F5FC"),
         "aws": ("#1F4FD8", "#F4F8FD"),
         "openai": ("#FF9900", "#FFF8F0")}

HALO = [pe.withStroke(linewidth=4.0, foreground="white")]


def zone(ax, x, y, w, h, label, kind):
    edge, face = ZONES[kind]
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle="round,pad=0.02,rounding_size=0.18", linewidth=2.2,
                 edgecolor=edge, facecolor=face, zorder=1))
    ax.text(x + 0.20, y + h - 0.30, label, fontsize=12.5, weight="bold",
            color=edge, va="center", ha="left", zorder=9,
            path_effects=[pe.withStroke(linewidth=4.5, foreground=face)])


def subzone(ax, x, y, w, h, label):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle="round,pad=0.02,rounding_size=0.12", linewidth=1.2,
                 edgecolor="#B9C3CF", facecolor="white", zorder=2))
    ax.text(x + 0.14, y + h - 0.22, label, fontsize=9.8, color="#5A6470",
            va="center", ha="left", zorder=9)


def box(ax, cx, cy, title, subtitle="", color=NEUTRAL, w=2.05, h=0.95):
    x, y = cx - w / 2, cy - h / 2
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                 boxstyle="round,pad=0.015,rounding_size=0.10", linewidth=1.5,
                 edgecolor=color, facecolor="white", zorder=4))
    ax.add_patch(FancyBboxPatch((x, y + h - 0.13), w, 0.13,
                 boxstyle="round,pad=0,rounding_size=0.02", linewidth=0,
                 facecolor=color, zorder=5))
    ax.text(cx, cy + (0.16 if subtitle else -0.02), title, fontsize=9.8,
            weight="bold", color=INK, ha="center", va="center", zorder=6)
    if subtitle:
        ax.text(cx, cy - 0.20, subtitle, fontsize=8.3, color="#4A545F",
                ha="center", va="center", zorder=6, linespacing=1.35)
    return {"x": cx, "y": cy, "w": w, "h": h}


def side(b, where):
    hw, hh = b["w"] / 2, b["h"] / 2
    return {"l": (b["x"] - hw, b["y"]), "r": (b["x"] + hw, b["y"]),
            "t": (b["x"], b["y"] + hh), "b": (b["x"], b["y"] - hh)}[where]


def edge(ax, a, aside, z, zside, color=GREY, style="-", lw=1.5, via=None,
         label="", lxy=None, fs=8.5, rad=0.0, zorder=3):
    """Arrow from side of box a to side of box z, optionally via waypoints."""
    start, end = side(a, aside), side(z, zside)
    pts = [start] + list(via or []) + [end]
    for i in range(len(pts) - 1):
        last = i == len(pts) - 2
        ax.add_patch(FancyArrowPatch(
            pts[i], pts[i + 1],
            arrowstyle="-|>" if last else "-", mutation_scale=14,
            linewidth=lw, linestyle=style, color=color, zorder=zorder,
            connectionstyle=f"arc3,rad={rad if len(pts) == 2 else 0}",
            shrinkA=0 if i else 2, shrinkB=2 if last else 0))
    if label:
        mx, my = lxy if lxy else ((start[0] + end[0]) / 2,
                                  (start[1] + end[1]) / 2)
        ax.text(mx, my, label, fontsize=fs, color=color, ha="center",
                va="center", zorder=10, linespacing=1.3, path_effects=HALO)


def build():
    fig, ax = plt.subplots(figsize=(19.2, 11.0))
    ax.set_xlim(0, 19.2); ax.set_ylim(0, 11.0); ax.axis("off")

    # ── heading ───────────────────────────────────────────────────────────
    ax.text(9.6, 10.62, "Insurance claims AI — two AI surfaces over one claim",
            fontsize=20, weight="bold", color=INK, ha="center", va="center")
    ax.text(9.6, 10.18, "Assist, don't decide:  no tool can settle a claim, "
            "and no tool returns a monetary figure",
            fontsize=11.5, color="#4A545F", ha="center", va="center",
            style="italic")
    legend = [(GREEN, "-", 2.8, "model traffic inside AWS — guardrailed"),
              (ORANGE, "--", 2.8, "direct to OpenAI — outside AWS, no guardrail possible"),
              (BLUE, ":", 2.0, "identity / credential")]
    x0 = 1.15
    for c, ls, lw, txt in legend:
        ax.plot([x0, x0 + 0.55], [9.68, 9.68], color=c, linestyle=ls,
                linewidth=lw, solid_capstyle="round")
        ax.text(x0 + 0.68, 9.68, txt, fontsize=9.4, color="#4A545F",
                va="center", ha="left")
        x0 += 0.68 + len(txt) * 0.104 + 0.70

    # ── zones.  bottom corridor y<2.10 is kept clear for the voice path ───
    zone(ax, 0.30, 3.30, 2.95, 5.85, "Browser  ·  untrusted", "browser")
    zone(ax, 3.75, 2.55, 10.85, 6.65, "AWS account", "aws")
    zone(ax, 15.30, 4.35, 3.65, 2.70, "OpenAI  ·  third party", "openai")
    ax.text(17.12, 7.35, "outside the AWS boundary", fontsize=8.6,
            color="#B36B00", ha="center", va="center", style="italic", zorder=9)

    subzone(ax, 4.05, 6.95, 2.40, 1.95, "Static delivery")
    subzone(ax, 9.05, 6.10, 5.30, 2.80, "Amazon Bedrock")
    subzone(ax, 9.05, 2.85, 2.55, 2.55, "State")

    # ── nodes ─────────────────────────────────────────────────────────────
    reviewer = box(ax, 1.72, 7.30, "Claims specialist", "review workspace",
                   NEUTRAL, w=2.25)
    caller = box(ax, 1.72, 4.55, "Provider", "voice caller", NEUTRAL, w=2.25)

    cdn = box(ax, 5.25, 8.20, "CloudFront", color=NETWORK, w=1.95, h=0.74)
    site = box(ax, 5.25, 7.40, "S3", "static client", STORAGE, w=1.95, h=0.74)
    cognito = box(ax, 5.25, 5.55, "Cognito", "JWT authorizer", SECURITY, w=1.95)

    api = box(ax, 7.75, 5.55, "API Gateway", "HTTP API · 12 routes",
              NETWORK, w=2.15)
    fn = box(ax, 7.75, 3.55, "Lambda", "Session + Tool Broker\n"
             "authorize · execute · audit", COMPUTE, w=2.35, h=1.20)

    guardrail = box(ax, 10.45, 7.95, "Bedrock Guardrail",
                    "content · PII · denied topics\nfails closed",
                    SECURITY, w=2.35, h=1.15)
    model = box(ax, 13.05, 7.95, "OpenAI gpt-5.6-terra",
                "Converse API, on Bedrock", ML, w=2.35, h=1.15)
    kb = box(ax, 11.75, 6.62, "Knowledge Base", "grounded retrieval",
             ML, w=2.35, h=0.74)

    data = box(ax, 10.32, 4.62, "DynamoDB", "claim · session", DATABASE,
               w=2.10, h=0.82)
    audit = box(ax, 10.32, 3.42, "DynamoDB · audit",
                "hash-chained, append-only\nby IAM · written first",
                DATABASE, w=2.30, h=1.00)
    secret = box(ax, 13.15, 4.62, "Secrets Manager",
                 "long-lived OpenAI key,\nread by Lambda at runtime",
                 SECURITY, w=2.35, h=0.92)
    logs = box(ax, 13.15, 3.42, "CloudWatch", "logs · tool latency", MGMT,
               w=2.35, h=0.82)

    realtime = box(ax, 17.12, 5.85, "OpenAI Realtime API",
                   "speech to speech, WebRTC", NEUTRAL, w=3.15, h=1.05)

    # ── page load and identity ────────────────────────────────────────────
    edge(ax, reviewer, "r", cdn, "l", GREY, lw=1.4, via=[(3.55, 7.30),
                                                        (3.55, 8.20)])
    edge(ax, cdn, "b", site, "t", GREY, ":", 1.3)
    edge(ax, reviewer, "b", cognito, "l", BLUE, ":", 1.5,
         via=[(1.72, 6.05), (3.55, 6.05), (3.55, 5.55)])
    edge(ax, caller, "r", cognito, "l", BLUE, ":", 1.5,
         via=[(3.55, 4.55), (3.55, 5.55)])
    edge(ax, cognito, "r", api, "l", BLUE, lw=1.8, label="verified JWT",
         lxy=(6.50, 5.78), fs=8.8)
    edge(ax, api, "b", fn, "t", GREY, lw=1.7)

    # ── claims review: inside AWS, screened both ways ─────────────────────
    edge(ax, fn, "t", guardrail, "l", GREEN, lw=2.8,
         via=[(7.75, 7.95)], label="review request", lxy=(8.10, 7.36), fs=9.2)
    edge(ax, guardrail, "r", model, "l", GREEN, lw=2.8,
         label="screened in and out", lxy=(11.75, 8.62), fs=8.8)
    edge(ax, fn, "r", kb, "l", GREEN, ":", 1.8,
         via=[(8.95, 3.55), (8.95, 6.62)], label="policy lookup",
         lxy=(9.90, 6.35), fs=8.6)

    # ── state, secret, telemetry ──────────────────────────────────────────
    edge(ax, fn, "r", data, "l", GREY, ":", 1.4, via=[(8.72, 4.62)])
    edge(ax, fn, "r", audit, "l", GREY, lw=1.7)
    edge(ax, audit, "r", logs, "l", GREY, ":", 1.3)

    # ── voice: token minted server-side, media leaves AWS entirely ────────
    edge(ax, fn, "b", realtime, "b", ORANGE, ":", 1.9,
         via=[(7.75, 2.72), (16.20, 2.72), (16.20, 4.10)],
         label="mint 10-minute\nephemeral token", lxy=(17.45, 3.30), fs=8.8)
    edge(ax, caller, "b", realtime, "b", ORANGE, "--", 3.0,
         via=[(1.72, 1.55), (17.85, 1.55), (17.85, 4.10)], zorder=8)
    ax.text(8.30, 1.24,
            "WebRTC audio, browser ↔ OpenAI — never traverses AWS, "
            "so no Bedrock guardrail can apply to it",
            fontsize=10.2, color="#B36B00", weight="bold", ha="center",
            va="center", zorder=10, path_effects=HALO)
    edge(ax, realtime, "t", api, "t", ORANGE, "--", 1.8,
         via=[(17.12, 9.55), (7.75, 9.55)], label="tool calls return to the broker",
         lxy=(12.45, 9.34), fs=8.8, zorder=6)

    out = HERE / "architecture.png"
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white",
                pad_inches=0.24)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    build()
