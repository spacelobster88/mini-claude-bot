import subprocess


def send_to_claude(prompt: str, continue_session: bool = False, timeout: int = 300) -> str:
    """Send a prompt to Claude CLI and return the text response."""
    args = ["claude", "-p", "--output-format", "text"]
    if continue_session:
        args.append("--continue")
    args.append(prompt)

    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    if result.returncode != 0 and result.stderr:
        return f"[ERROR] {result.stderr.strip()}"

    return result.stdout.strip() or "(empty response)"
