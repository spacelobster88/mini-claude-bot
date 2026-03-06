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

BILINGUAL FORMAT: Interleave English and Chinese PARAGRAPH BY PARAGRAPH. After each English paragraph, immediately put its Chinese translation in \begin{cntranslation}...\end{cntranslation}. Then continue with the next English paragraph, followed by its Chinese translation, and so on. Do NOT write all English first then all Chinese — alternate every paragraph.

Example:

\section{Daily Wisdom}
``The only way to do great work is to love what you do.'' — Steve Jobs
\begin{cntranslation}
「做出伟大工作的唯一方法，就是热爱你所做的事。」—— 史蒂夫·乔布斯
\end{cntranslation}

The key insight here is that passion drives excellence in every field.
\begin{cntranslation}
关键启示是：热情驱动每个领域的卓越表现。
\end{cntranslation}

RULES:
- No \documentclass, \begin{document}, \end{document}
- Use \href{URL}{Display Text} for links — never raw URLs
- Escape: \& \% \$ \# \_ \{ \}
- Use \textbf{} \textit{} for emphasis
- Alternate English paragraph → \begin{cntranslation}Chinese\end{cntranslation} → English paragraph → \begin{cntranslation}Chinese\end{cntranslation}, throughout EVERY section

SECTIONS (use these exact names, in this order):

\section{Daily Tech Learning}
Pick ONE keyword/concept from each of the 3 topics below. Use a TOP-DOWN approach: start from the big picture (what is the system, what problem does it solve, how the pieces fit together) before zooming into details. Think system-level and architecture-level, not scattered trivia. Start from beginner-level concepts first; do NOT pick advanced topics unless requested. For each keyword, write ~50 words in English + ~50 words in Chinese translation. Keep it clear, memorable, and practical. Use everyday analogies. No diagrams or images.
1. \textbf{AI Infra} — Start top-down: the overall ML compute stack (hardware → framework → serving), then progressively cover each layer. e.g., Why GPU for ML → training pipeline architecture → model serving system → optimization techniques.
2. \textbf{ML Platform} — Start top-down: what is an ML platform and why teams need one, then progressively cover each component. e.g., ML lifecycle overview → experiment tracking → model registry → deployment → monitoring.
3. \textbf{Data Pipeline} — Start top-down: how data flows from source to insight, then progressively cover each stage. e.g., Data architecture overview → ingestion → storage → transformation → serving.
Use \href{url}{title} to cite one source per topic if relevant.

\section{Political \& Economic Trends}
Key US and international political/economic developments. Each item: \href{url}{headline} followed by a 20-30 word summary. Keep it concise. This section is ENGLISH ONLY — do NOT include \begin{cntranslation} Chinese translation.

\section{My Portfolio Watch}
Analyze the following stocks I currently hold: ORCL (8), MSFT (5), AVGO (2), INTC (6), TSLA (2.5), NFLX (1), PLTR (4), NVDA (0.1), MU (0.4), GOOGL (8.2), META (3), COIN (7.5), ISRG (8).
For each stock:
- Current price and recent movement (search for today's data)
- Buy/hold/sell recommendation with brief reasoning
- Any notable news, earnings, analyst upgrades/downgrades, or events worth watching
Use a table format where possible. Highlight actionable insights with \textbf{}.

\section{Healthy Living}
One practical wellness tip with scientific backing.

\section{Daily Wisdom \& Love}
One inspiring quote with attribution, followed by a short sweet personal note (1-2 sentences) to the reader. This section is ENGLISH ONLY — do NOT include \begin{cntranslation} Chinese translation.

Search the web for today's ACTUAL news. Use REAL article URLs, not homepages."""

LOVE_NOTE_DEFAULT = "Wishing you a beautiful day filled with joy."


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
        ["xelatex", "-interaction=nonstopmode", "-output-directory", str(output_dir), str(tex_path)],
        capture_output=True, text=True, timeout=120,
    )
    pdf_path = output_dir / tex_path.with_suffix(".pdf").name
    if not pdf_path.exists():
        print(f"XeLaTeX failed:\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return pdf_path


SEND_QUEUE = SCRIPT_DIR.parent / "output" / "pending_email.json"
SEND_JXA = SCRIPT_DIR / "send_email.js"


def send_email(to: str, cc: str, bcc: str, subject: str, body: str, attachment: str) -> None:
    """Send email via Mail.app using JXA (JavaScript for Automation).

    Writes email params to a JSON queue file, then runs the JXA script
    via osascript. The JXA script reads the queue, sends via Mail.app,
    and deletes the queue file on success.
    """
    params = {
        "sender": SENDER,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
        "attachment": attachment,
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
    content = sanitize_latex(run_claude(CN_PROMPT))

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


def generate_english_report(preview: bool = False):
    now_la = datetime.now(timezone(timedelta(hours=-8)))
    date_str = now_la.strftime("%B %d, %Y")
    date_str_file = now_la.strftime("%Y-%m-%d")

    print(f"Generating English report for {date_str}...")
    content = sanitize_latex(run_claude(EN_PROMPT))

    # Load template
    template = (TEMPLATE_DIR / "english.tex").read_text()
    tex = (template
           .replace("<<DATE>>", date_str)
           .replace("<<CONTENT>>", content))

    # Write and compile
    tex_path = OUTPUT_DIR / f"daily_en_{date_str_file}.tex"
    tex_path.write_text(tex)
    pdf_path = compile_pdf(tex_path, OUTPUT_DIR)
    print(f"PDF generated: {pdf_path}")

    subject = f"Daily Intelligence Report - {date_str}"
    body = f"Good morning!\n\nYour daily report is attached.\n\n---\nAuto-generated by mini-claude-bot"

    if preview:
        send_email(to=CONTACTS["cc"], cc="", bcc="", subject=f"[PREVIEW] {subject}", body=body, attachment=str(pdf_path.resolve()))
    else:
        send_email(to=CONTACTS["secondary"], cc=CONTACTS["cc"], bcc="", subject=subject, body=body, attachment=str(pdf_path.resolve()))


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
