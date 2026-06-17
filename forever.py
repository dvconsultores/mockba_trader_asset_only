import subprocess
import time
import sys
import os
from datetime import datetime

# List of scripts to run (relative to project root)
scripts = [
    "telegram.py"
]

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    sys.stdout.flush()  # Ensure logs appear in real-time (e.g., in Docker)

def run_script(script_path):
    """Start a script as a subprocess."""
    log(f"🚀 Starting {script_path}")
    # Inherit parent stdout/stderr — avoid pipe buffer deadlock.
    # The child writes its own log file; we just need to monitor its exit.
    return subprocess.Popen(
        [sys.executable, script_path],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

def main():
    processes = {}

    # Start all scripts
    for script in scripts:
        if not os.path.exists(script):
            log(f"❌ Script not found: {script} — skipping!")
            continue
        processes[script] = run_script(script)

    log(f"✅ Supervisor started with {len(processes)} bots.")

    try:
        while True:
            for script, proc in list(processes.items()):
                if proc.poll() is not None:  # Process exited
                    return_code = proc.returncode
                    log(f"⚠️ {script} exited with code {return_code}. Restarting in 3s...")

                    time.sleep(3)
                    processes[script] = run_script(script)

            time.sleep(3)  # Check every 3 seconds

    except KeyboardInterrupt:
        log("🛑 Received SIGINT. Shutting down all bots...")
        for proc in processes.values():
            proc.terminate()
        for proc in processes.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log("✅ All bots stopped.")
        sys.exit(0)

if __name__ == "__main__":
    main()