#!/usr/bin/env python3
"""
Daily report generator — uses Claude CLI to research + write LaTeX content,
compiles to PDF, and sends via macOS Mail.app.

Usage:
    python generate_report.py --lang cn   # Chinese report
    python generate_report.py --lang en   # English report
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TEMPLATE_DIR = SCRIPT_DIR.parent / "templates"
OUTPUT_DIR = SCRIPT_DIR.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Contacts — set via environment variables
CONTACTS = {
    "primary": os.environ.get("REPORT_TO_PRIMARY", ""),
    "cc": os.environ.get("REPORT_CC", ""),
    "secondary": os.environ.get("REPORT_TO_SECONDARY", ""),
}
SENDER = os.environ.get("REPORT_SENDER", "")

CN_PROMPT = r"""Generate a Chinese daily report as LaTeX body content. Search the web for today's real news.

CRITICAL: Output RAW LaTeX ONLY. No markdown, no code blocks, no explanations, no notes. Start directly with \section and end with the last \end{itemize} or text. Nothing else.

RULES:
- No \documentclass, \begin{document}, \end{document}
- Use \href{URL}{显示文字} for links — never raw URLs
- Escape: \& \% \$ \# \_ \{ \}
- Use \textcolor{riskred}{text} and \textcolor{growgreen}{text}
- Use \textbf{} \textit{} for emphasis

SECTIONS (use these exact names, in this order):

\section{A股与港股市场分析}
Major indices (上证, 深证, 恒生), notable movers, sector performance. Real data + \href links to sources.

\section{风险与机遇评估}
Risks in \textcolor{riskred}{红色}, opportunities in \textcolor{growgreen}{绿色}.

\section{每日励志}
One inspiring quote with attribution.

\section{科技前沿}
Latest tech news. Search 36氪, 虎嗅, 钛媒体. Use \href{url}{标题} for every source.

\section{金融概念深度解析}
Pick one financial concept, explain clearly for general audience.

\section{养生健康建议}
One practical health tip with scientific backing.

\section{科技新闻速递}
3-5 top tech news as \href{url}{headline} items.

\section{政经动态}
Political/economic news from 新浪财经, 东方财富, 香港信报. \href{url}{headline} links.

Search the web for today's ACTUAL news. Use REAL article URLs, not homepages."""

EN_PROMPT = r"""Generate a BILINGUAL (English-Chinese) daily report as LaTeX body content. Search the web for today's real news.

CRITICAL: Output RAW LaTeX ONLY. No markdown, no code blocks, no explanations, no notes. Start directly with \section and end with the LOVENOTE line. Nothing else.

LANGUAGE RULES PER SECTION — follow these STRICTLY:
- Career Growth: English paragraphs 1-3 are ENGLISH ONLY. Paragraph 4 is CHINESE ONLY.
- Politics \& Economics: ENGLISH ONLY. No Chinese at all.
- Stock Watch: CHINESE ONLY. No English at all. No \begin{cntranslation}.
- Healthy Life: CHINESE ONLY. No English at all. No \begin{cntranslation}.
- Wisdom \& Love: ENGLISH ONLY. No Chinese at all. No \begin{cntranslation}.

RULES:
- No \documentclass, \begin{document}, \end{document}
- Use \href{URL}{Display Text} for links — never raw URLs
- Escape: \& \% \$ \# \_ \{ \}
- Use \textbf{} \textit{} for emphasis
- Use EXACT section names as specified below — do NOT rename them

SECTIONS (use these exact names, in this order):

\section{Career Growth}
Pick ONE keyword/concept from each of the 3 topics below. Use a TOP-DOWN approach: start from the big picture (what is the system, what problem does it solve, how the pieces fit together) before zooming into details. Think system-level and architecture-level, not scattered trivia. Start from beginner-level concepts first; do NOT pick advanced topics unless requested. For each keyword, write ~50 words in English. Keep it clear, memorable, and practical. Use everyday analogies. No diagrams or images. The 3 English paragraphs are ENGLISH ONLY — do NOT include \begin{cntranslation} Chinese translation for them.
1. \textbf{AI Infra} — Start top-down: the overall ML compute stack (hardware → framework → serving), then progressively cover each layer. e.g., Why GPU for ML → training pipeline architecture → model serving system → optimization techniques.
2. \textbf{ML Platform} — Start top-down: what is an ML platform and why teams need one, then progressively cover each component. e.g., ML lifecycle overview → experiment tracking → model registry → deployment → monitoring.
3. \textbf{Data Pipeline} — Start top-down: how data flows from source to insight, then progressively cover each stage. e.g., Data architecture overview → ingestion → storage → transformation → serving.
Use \href{url}{title} to cite one source per topic if relevant.
4. \textbf{程序员职场小技巧} — 用中文写三句话，讲一个实用的程序员职场生存/成长技巧。话题可以包括：如何跟PM沟通、code review技巧、向上管理、面试准备、时间管理、职业发展等。This paragraph is CHINESE ONLY.

\section{Politics \& Economics}
Pick the TOP 3 most important US and international political/economic news of the day. Use bullet points (\\begin{itemize}), NOT numbered lists. Each item: \href{url}{headline} followed by ONE sentence summary only — no extra details or elaboration. Only 3 items, no more. This section is ENGLISH ONLY — do NOT include \begin{cntranslation} Chinese translation.

\section{Stock Watch}
我持有以下股票：ORCL (8), MSFT (5), AVGO (2), INTC (6), TSLA (2.5), NFLX (1), PLTR (4), NVDA (0.1), MU (0.4), GOOGL (8.2), META (3), COIN (7.5), ISRG (8).
从中挑出最多5个你认为今天最值得关注/操作的股票（可以少于5个）。用bullet point格式。每个股票格式统一为：股票代码（当前价格, 当日涨跌幅%）— 建议。然后给出机构目标价和一句话理由。确保每只股票都有当前价格和涨跌幅，格式一致。只用中文，不需要英文。This section is CHINESE ONLY — do NOT include English text or \begin{cntranslation}.

\section{Healthy Life}
给出三条健康建议，每条不超过三句话。第一条「今日晚餐🍽️」：给两个选项。Option A中餐：每天给五个菜名，只写菜名不写做法，不需要考虑制作时间和复杂度，只需要营养均衡健康。Option B非中餐（美国菜、意大利菜、印度菜等，要在家容易做的）：写明菜名、主要食材和简单做法。周一到周五简单快手相对健康，周六周日丰盛复杂不必考虑健康。第二条「每日一练🧘🏻‍♀️」：一条针对改善驼背、腰痛、体态的通用小建议（不超过三句话），然后搜索YouTube找一个10-15分钟的改善驼背/腰痛/体态的跟练视频，用\href{url}{视频标题}附上链接。每天推荐不同的视频。第三条「健康小贴士🥦」：一条通用的健康生活建议。只用中文。This section is CHINESE ONLY — do NOT include English text or \begin{cntranslation}.

\section{Wisdom \& Love}
One inspiring quote with attribution. Then on the next line, write a short sweet personal love note (1-2 sentences) in pink using \textcolor{heartpink}{...}. This section is ENGLISH ONLY — do NOT include \begin{cntranslation} Chinese translation.

Search the web for today's ACTUAL news. Use REAL article URLs, not homepages."""

LOVE_NOTE_DEFAULT = "Wishing you a beautiful day filled with joy."

# Regex matching emoji characters (Unicode emoji ranges)
_EMOJI_RE = re.compile(
    "["
    "\U0000200D"          # zero-width joiner
    "\U0000FE0F"          # variation selector
    "\U00002600-\U000027BF"  # misc symbols
    "\U0001F300-\U0001F9FF"  # emoticons, symbols, etc.
    "\U0001FA00-\U0001FAFF"  # extended-A
    "\U0001F3FB-\U0001F3FF"  # skin tone modifiers
    "]+",
    flags=re.UNICODE,
)

# Skin tone modifiers and ZWJ sequences break in LuaTeX — strip them to base emoji
_ZWJ_STRIP_RE = re.compile(
    "[\U0001F3FB-\U0001F3FF]"  # skin tone modifiers
    "|[\U0000200D]."           # ZWJ + next char (gender/hair modifiers)
    "|[\U0000FE0F]",           # variation selectors
    flags=re.UNICODE,
)


def wrap_emojis_for_latex(text: str) -> str:
    """Wrap emoji characters with {\\emojifont ...} for LuaLaTeX rendering.

    Strips ZWJ sequences and skin tone modifiers that LuaTeX cannot handle,
    keeping only the base emoji.
    """
    def _wrap(m: re.Match) -> str:
        emoji = _ZWJ_STRIP_RE.sub("", m.group())
        if not emoji:
            return ""
        return r"{\emojifont " + emoji + "}"
    return _EMOJI_RE.sub(_wrap, text)


def run_claude(prompt: str) -> str:
    """Send prompt to Claude CLI and get response."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        [
            "claude", "-p",
            "--output-format", "text",
            "--allowedTools", "WebSearch,WebFetch",
        ],
        input=prompt,
        capture_output=True, text=True, timeout=600, env=env,
    )
    if result.returncode != 0:
        print(f"Claude CLI error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def sanitize_latex(text: str) -> str:
    """Strip conversational noise from Claude output, keeping only LaTeX body content."""
    # Extract from markdown code block if present
    m = re.search(r"```(?:latex)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)

    lines = text.split("\n")

    # Find first line that looks like LaTeX content
    start = 0
    for i, line in enumerate(lines):
        if re.match(r"\\(section|subsection|begin|noindent|textbf|paragraph)", line.strip()):
            start = i
            break

    # Find last line that looks like LaTeX content
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if re.match(r"\\(section|end|item|href|textbf|textit|textcolor|noindent|hfill|vspace)", stripped) \
                or stripped.startswith("LOVENOTE:") \
                or stripped == "" \
                or re.match(r"^[^*A-Z]", stripped):  # likely LaTeX continuation
            end = i + 1
            break

    # Fallback: if we found no LaTeX, strip obvious non-LaTeX lines
    content = "\n".join(lines[start:end])

    # Remove any remaining markdown artifacts
    content = re.sub(r"^\*\*.*?\*\*\s*$", "", content, flags=re.MULTILINE)  # **bold notes**
    content = re.sub(r"^>\s+.*$", "", content, flags=re.MULTILINE)  # > blockquotes
    content = re.sub(r"\n{3,}", "\n\n", content)

    return content.strip()


def extract_love_note(content: str) -> tuple[str, str]:
    """Extract LOVENOTE: line from content, return (content_without_note, love_note)."""
    lines = content.split("\n")
    note = LOVE_NOTE_DEFAULT
    filtered = []
    for line in lines:
        if line.strip().startswith("LOVENOTE:"):
            note = line.strip().replace("LOVENOTE:", "").strip()
        else:
            filtered.append(line)
    return "\n".join(filtered), note


def compile_pdf(tex_path: Path, output_dir: Path) -> Path:
    """Compile .tex to PDF using XeLaTeX."""
    result = subprocess.run(
        ["lualatex", "-interaction=nonstopmode", "-output-directory", str(output_dir), str(tex_path)],
        capture_output=True, text=True, timeout=120,
    )
    pdf_path = output_dir / tex_path.with_suffix(".pdf").name
    if not pdf_path.exists():
        print(f"XeLaTeX failed:\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return pdf_path


SEND_QUEUE = SCRIPT_DIR.parent / "output" / "pending_email.json"
SEND_JXA = SCRIPT_DIR / "send_email.js"


def send_email(to: str, cc: str, bcc: str, subject: str, body: str, attachment: str, reply_to_subject: str = "") -> None:
    """Send email via Mail.app using JXA (JavaScript for Automation).

    Writes email params to a JSON queue file, then runs the JXA script
    via osascript. The JXA script reads the queue, sends via Mail.app,
    and deletes the queue file on success.

    If reply_to_subject is set, the script will search the Sent mailbox
    for a message with that subject and reply to it (for email threading).
    """
    params = {
        "sender": SENDER,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
        "attachment": attachment,
        "reply_to_subject": reply_to_subject,
    }
    SEND_QUEUE.write_text(json.dumps(params, ensure_ascii=False))

    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(SEND_JXA)],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        print(f"Email send failed: {result.stderr}", file=sys.stderr)
        print("Queue file preserved for manual retry: " + str(SEND_QUEUE), file=sys.stderr)
        _notify_failure(f"邮件发送失败 ({to}): {result.stderr[:200]}")
    elif SEND_QUEUE.exists():
        print("Email may not have been sent (queue file still exists)", file=sys.stderr)
        _notify_failure(f"邮件可能未发送 ({to}): queue 文件仍存在")
    else:
        print(f"Email sent to {to}" + (f", cc {cc}" if cc else "") + (f", bcc {bcc}" if bcc else ""))


TELEGRAM_BOT_TOKEN = "8640999049:AAFhaP7s2zcSCNO9RI4ev1fpRSVwNsCmSak"
TELEGRAM_CHAT_ID = "6838572051"


def _notify_failure(msg: str) -> None:
    """Send failure notification via Telegram."""
    try:
        import urllib.request
        import urllib.parse
        text = f"⚠️ Report Email Error\n\n{msg}\n\n手动补发: osascript -l JavaScript reports/scripts/send_email.js"
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception:
        pass


def generate_chinese_report(preview: bool = False):
    now_shanghai = datetime.now(timezone(timedelta(hours=8)))
    date_str = now_shanghai.strftime("%Y年%m月%d日")
    date_str_file = now_shanghai.strftime("%Y-%m-%d")

    print(f"Generating Chinese report for {date_str}...")
    content = wrap_emojis_for_latex(sanitize_latex(run_claude(CN_PROMPT)))

    # Load template
    template = (TEMPLATE_DIR / "chinese.tex").read_text()
    tex = template.replace("<<DATE>>", date_str).replace("<<CONTENT>>", content)

    # Write and compile
    tex_path = OUTPUT_DIR / f"daily_cn_{date_str_file}.tex"
    tex_path.write_text(tex)
    pdf_path = compile_pdf(tex_path, OUTPUT_DIR)
    print(f"PDF generated: {pdf_path}")

    # Plain text version (strip LaTeX commands roughly)
    plain = content
    plain = re.sub(r"\\section\{([^}]*)\}", r"\n=== \1 ===\n", plain)
    plain = re.sub(r"\\subsection\{([^}]*)\}", r"\n--- \1 ---\n", plain)
    plain = re.sub(r"\\href\{[^}]*\}\{([^}]*)\}", r"\1", plain)
    plain = re.sub(r"\\textbf\{([^}]*)\}", r"\1", plain)
    plain = re.sub(r"\\textit\{([^}]*)\}", r"\1", plain)
    plain = re.sub(r"\\textcolor\{[^}]*\}\{([^}]*)\}", r"\1", plain)
    plain = re.sub(r"\\item\s*", "• ", plain)
    plain = re.sub(r"\\begin\{[^}]*\}", "", plain)
    plain = re.sub(r"\\end\{[^}]*\}", "", plain)
    plain = re.sub(r"\\[a-zA-Z]+", "", plain)
    plain = re.sub(r"[{}]", "", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)

    subject = f"每日智能报告 - {date_str}"
    body = f"每日智能报告 - {date_str}\n\n{plain.strip()}\n\n---\n由 mini-claude-bot 自动生成"

    if preview:
        send_email(to=CONTACTS["cc"], cc="", bcc="", subject=f"[PREVIEW] {subject}", body=body, attachment=str(pdf_path.resolve()))
    else:
        send_email(to=CONTACTS["primary"], cc=CONTACTS["cc"], bcc="", subject=subject, body=body, attachment=str(pdf_path.resolve()))


def get_sunnyvale_weather() -> str:
    """Get today's weather for Sunnyvale via Claude CLI."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        [
            "claude", "-p",
            "--output-format", "text",
            "--allowedTools", "WebSearch,WebFetch",
        ],
        input="Search for today's weather in Sunnyvale, CA. Reply with ONE short sentence: temperature range (°F), conditions, and clothing suggestions for morning, afternoon, AND evening. Example: 'Sunny, 58-72°F — light jacket in the morning, t-shirt by afternoon, bring a sweater for the evening.' Nothing else.",
        capture_output=True, text=True, timeout=60, env=env,
    )
    if result.returncode != 0:
        return "Check the weather before heading out!"
    raw = result.stdout.strip()
    # Strip any "Sources:" or similar trailing sections from Claude output
    for marker in ["Sources:", "Source:", "References:", "Note:"]:
        idx = raw.find(marker)
        if idx > 0:
            raw = raw[:idx].strip()
    # Only keep the first line/sentence
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    return lines[0] if lines else "Check the weather before heading out!"


WEEKLY_SUBJECT_FILE = SCRIPT_DIR.parent / "output" / "weekly_en_subject.json"
WEEKLY_PREVIEW_SUBJECT_FILE = SCRIPT_DIR.parent / "output" / "weekly_en_preview_subject.json"


def _get_weekly_subject(now_la, preview: bool = False) -> tuple[str, str]:
    """Return (subject, reply_to_subject) for weekly email threading.

    Production: Monday starts new thread, Tue-Sun reply to previous day.
    Preview: separate thread from production. New thread after Sunday midnight PT.
    Both reply to the previous email in their respective thread.
    """
    monday = now_la - timedelta(days=now_la.weekday())
    week_label = monday.strftime('%b %d, %Y')

    if preview:
        state_file = WEEKLY_PREVIEW_SUBJECT_FILE
        today_subject = f"[Auto Test] Erin's Daily\U0001F4F0"
    else:
        state_file = WEEKLY_SUBJECT_FILE
        today_subject = f"Erin's Daily\U0001F4F0"

    # Check if we need a new thread (Monday, or week changed since last send)
    start_new = now_la.weekday() == 0  # Monday
    reply_to = ""

    if state_file.exists():
        saved = json.loads(state_file.read_text())
        saved_week = saved.get("week_label", "")
        if saved_week != week_label:
            start_new = True  # New week since last send
        elif not start_new:
            reply_to = saved.get("last_subject", "")

    state_file.write_text(json.dumps({
        "last_subject": today_subject,
        "week_label": week_label,
    }))
    return today_subject, reply_to


def generate_english_report(preview: bool = False):
    now_la = datetime.now(timezone(timedelta(hours=-8)))
    date_str = now_la.strftime("%B %d, %Y")
    date_str_file = now_la.strftime("%Y-%m-%d")
    day_of_week = now_la.strftime("%A")

    print(f"Generating English report for {date_str}...")
    content = wrap_emojis_for_latex(sanitize_latex(run_claude(EN_PROMPT)))

    # Pick a random dog for the header
    import random
    dogs = [
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogHappy \end{scope}",
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogHeart \end{scope}",
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogSleepy \end{scope}",
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogPair \end{scope}",
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogWave \end{scope}",
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogHearts \end{scope}",
        r"\begin{scope}[shift={(\textwidth-1.2cm, 1.4cm)}] \dogCookie \end{scope}",
    ]
    random.seed(now_la.toordinal())  # same dog for same day
    dog_tikz = random.choice(dogs)

    # Load template
    template = (TEMPLATE_DIR / "english.tex").read_text()
    assets_dir = str(TEMPLATE_DIR.parent / "assets")
    tex = (template
           .replace("<<DATE>>", date_str)
           .replace("<<DOG_TIKZ>>", dog_tikz)
           .replace("<<ASSETS_DIR>>", assets_dir)
           .replace("<<CONTENT>>", content))

    # Write and compile
    tex_path = OUTPUT_DIR / f"daily_en_{date_str_file}.tex"
    tex_path.write_text(tex)
    pdf_path = compile_pdf(tex_path, OUTPUT_DIR)
    print(f"PDF generated: {pdf_path}")

    # Get weather for email body
    weather = get_sunnyvale_weather()

    # Weekly threading (separate threads for preview and production)
    subject, reply_to_subject = _get_weekly_subject(now_la, preview=preview)

    body = (
        f"Dear Erin\U0001F427,\n\n"
        f"Good morning.\U00002600\U0000FE0F\n\n"
        f"Today is {day_of_week}, {date_str}. {weather}\n\n"
        f"Fight on and have a nice day.\U0001F4AA\U0001F3FC\U0000263A\U0000FE0F\n\n"
        f"Love,\nEddie\U0001F436"
    )

    if preview:
        send_email(to=CONTACTS["secondary"], cc=CONTACTS["cc"], bcc="", subject=subject, body=body, attachment=str(pdf_path.resolve()), reply_to_subject=reply_to_subject)
    else:
        send_email(to=CONTACTS["secondary"], cc=CONTACTS["cc"], bcc="", subject=subject, body=body, attachment=str(pdf_path.resolve()), reply_to_subject=reply_to_subject)


def main():
    parser = argparse.ArgumentParser(description="Generate daily report")
    parser.add_argument("--lang", choices=["cn", "en"], required=True, help="Report language")
    parser.add_argument("--preview", action="store_true", help="Send to cc recipient only for preview")
    args = parser.parse_args()

    if args.lang == "cn":
        generate_chinese_report(preview=args.preview)
    else:
        generate_english_report(preview=args.preview)


if __name__ == "__main__":
    main()
