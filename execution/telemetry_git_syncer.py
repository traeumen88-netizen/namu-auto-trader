"""
Telemetry Git Syncer (execution/telemetry_git_syncer.py)
Dedicated background worker that commits and pushes data/live_telemetry to GitHub.
- Non-blocking: Trading engine is NEVER blocked by git operations or network errors
- 5-minute periodic sync batching
- Fast-sync (within 30s) on critical events (BUY_FILLED, SELL_FILLED, BROKER_REJECT, etc.)
- Mutex & File lock: data/live_telemetry/.git_sync.lock
- Strict pre-push leak scanner: blocks push if secrets or non-telemetry files are staged
- Isolated error recovery: updates git_sync_status to DEGRADED without crashing
"""

import os
import time
import logging
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

from core.telemetry_sanitizer import TelemetrySanitizer

logger = logging.getLogger("TelemetryGitSyncer")

KST = timezone(timedelta(hours=9))


class TelemetryGitSyncer:
    """Synchronizes data/live_telemetry/ directory to GitHub repository in background."""

    def __init__(
        self,
        repo_dir: str = ".",
        telemetry_dir: str = "data/live_telemetry",
        sync_interval_sec: float = 300.0,
        fast_sync_debounce_sec: float = 30.0,
        remote_name: str = "origin",
        branch_name: str = "main",
        exporter=None,
    ):
        self.repo_dir = os.path.abspath(repo_dir)
        self.telemetry_dir = os.path.abspath(os.path.join(self.repo_dir, telemetry_dir))
        self.sync_interval_sec = sync_interval_sec
        self.fast_sync_debounce_sec = fast_sync_debounce_sec
        self.remote_name = remote_name
        self.branch_name = branch_name
        self.exporter = exporter

        self.lock_file_path = os.path.join(self.telemetry_dir, ".git_sync.lock")
        self._thread_lock = threading.Lock()

        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

        self._fast_sync_event = threading.Event()
        self._last_fast_sync_trigger: float = 0.0
        self._pending_critical_reason: str = ""

        self.last_push_at: Optional[str] = None
        self.last_push_result: str = "NONE"
        self.git_sync_status: str = "OK"

    def start(self):
        """Starts the background syncer thread."""
        with self._thread_lock:
            if self._running:
                return
            self._running = True
            self._worker_thread = threading.Thread(target=self._run_loop, name="TelemetryGitSyncerThread", daemon=True)
            self._worker_thread.start()
            logger.info("TelemetryGitSyncer thread started.")

    def stop(self):
        """Stops the background worker thread."""
        self._running = False
        self._fast_sync_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
        self._release_file_lock()

    def trigger_critical_sync(self, reason: str = ""):
        """Triggers a fast sync within the debounce window (default 30s)."""
        logger.info(f"Critical telemetry event received: {reason}. Scheduling fast git sync.")
        self._pending_critical_reason = reason
        self._last_fast_sync_trigger = time.time()
        self._fast_sync_event.set()

    def _acquire_file_lock(self) -> bool:
        """Acquires lock file to avoid concurrent git commits."""
        os.makedirs(self.telemetry_dir, exist_ok=True)
        try:
            if os.path.exists(self.lock_file_path):
                # Check for stale lock (older than 180 seconds)
                try:
                    mtime = os.path.getmtime(self.lock_file_path)
                    if (time.time() - mtime) > 180.0:
                        logger.warning("Stale git sync lock file detected. Removing stale lock.")
                        os.remove(self.lock_file_path)
                    else:
                        return False
                except Exception:
                    pass

            with open(self.lock_file_path, "x") as f:
                f.write(f"pid={os.getpid()}, ts={datetime.now(KST).isoformat()}\n")
            return True
        except FileExistsError:
            return False
        except Exception as e:
            logger.warning(f"Failed to acquire git sync lock: {e}")
            return False

    def _release_file_lock(self):
        """Releases the lock file."""
        try:
            if os.path.exists(self.lock_file_path):
                os.remove(self.lock_file_path)
        except Exception as e:
            logger.warning(f"Error releasing git sync lock: {e}")

    def _run_loop(self):
        """Background loop for periodic and fast synchronization."""
        last_sync_time = time.time()

        while self._running:
            try:
                # Wait for interval or fast sync event
                triggered = self._fast_sync_event.wait(timeout=self.fast_sync_debounce_sec)

                if not self._running:
                    break

                now = time.time()
                should_sync = False
                reason = "periodic"

                if triggered:
                    self._fast_sync_event.clear()
                    # Wait for debounce duration from trigger
                    elapsed_since_trigger = now - self._last_fast_sync_trigger
                    if elapsed_since_trigger < self.fast_sync_debounce_sec:
                        time.sleep(min(self.fast_sync_debounce_sec - elapsed_since_trigger, 5.0))
                    should_sync = True
                    reason = self._pending_critical_reason or "critical_event"
                    self._pending_critical_reason = ""
                elif (now - last_sync_time) >= self.sync_interval_sec:
                    should_sync = True
                    reason = "periodic"

                if should_sync:
                    self.sync_now(reason=reason)
                    last_sync_time = time.time()

            except Exception as loop_err:
                logger.error(f"Error in TelemetryGitSyncer loop: {loop_err}", exc_info=True)
                time.sleep(5.0)

    def sync_now(self, reason: str = "manual") -> bool:
        """
        Executes git commit and push safely without blocking or raising.
        Returns True if push succeeded or nothing to commit, False if failed.
        """
        if not self._acquire_file_lock():
            logger.info("Git sync already in progress or locked by another worker. Skipping this cycle.")
            return False

        try:
            return self._perform_git_sync(reason)
        except Exception as e:
            logger.error(f"Git sync failed with unexpected error: {e}", exc_info=True)
            self._update_status(status="DEGRADED", result=f"ERROR: {e}")
            return False
        finally:
            self._release_file_lock()

    def _perform_git_sync(self, reason: str) -> bool:
        """Internal staged git commit and push execution."""
        now_dt = datetime.now(KST)
        time_str = now_dt.strftime("%Y-%m-%d %H:%M")

        # 1. Flush any in-flight exporter events first
        if self.exporter:
            try:
                self.exporter.flush()
            except Exception as fl_err:
                logger.warning(f"Error flushing exporter before git sync: {fl_err}")

        # 2. Stage ONLY data/live_telemetry/
        rel_telemetry_dir = os.path.relpath(self.telemetry_dir, self.repo_dir).replace("\\", "/")
        code, out, err = self._run_git(["add", rel_telemetry_dir])
        if code != 0:
            logger.error(f"git add failed: {err}")
            self._update_status(status="DEGRADED", result=f"GIT_ADD_FAILED: {err}")
            return False

        # 3. Check staged changes
        code, staged_files_out, _ = self._run_git(["diff", "--name-only", "--cached"])
        staged_files = [f.strip() for f in staged_files_out.splitlines() if f.strip()]
        if not staged_files:
            logger.debug("No telemetry changes to commit.")
            self._update_status(status="OK", result="UP_TO_DATE")
            return True

        # 4. Security & Leak validation
        full_staged_paths = [os.path.join(self.repo_dir, f) for f in staged_files]
        is_clean, violations = TelemetrySanitizer.scan_files_for_push(full_staged_paths)

        if not is_clean:
            logger.error(f"🚨 PUSH BLOCKED - SECRET_DETECTED! Violations: {violations}")
            self._run_git(["reset", "HEAD", "--", rel_telemetry_dir])
            self._update_status(status="DEGRADED", result=f"PUSH_BLOCKED: {violations[0]}")
            if self.exporter:
                self.exporter.record_error("SECRET_DETECTED", f"Push blocked due to leaks: {violations}")
            return False

        # Ensure NO file outside live_telemetry is staged
        for sf in staged_files:
            sf_norm = sf.replace("\\", "/")
            sf_full = os.path.abspath(os.path.join(self.repo_dir, sf))
            is_inside = (
                sf_full.startswith(self.telemetry_dir)
                or sf_norm.startswith(rel_telemetry_dir)
                or "data/live_telemetry" in sf_norm
            )
            if not is_inside:
                logger.error(f"🚨 PUSH BLOCKED: Non-telemetry file staged! {sf}")
                self._run_git(["reset", "HEAD"])
                self._update_status(status="DEGRADED", result=f"PUSH_BLOCKED_UNAUTHORIZED_FILE: {sf}")
                return False

        # 5. Format commit message
        if "FILL" in reason.upper():
            commit_msg = f"telemetry: LIVE SELL/FILL event {time_str}"
        elif any(k in reason.upper() for k in ("ERROR", "REJECT", "CIRCUIT", "MISMATCH")):
            commit_msg = f"telemetry: LIVE broker error {time_str}"
        else:
            commit_msg = f"telemetry: live update {time_str}"

        # 6. Commit
        code, c_out, c_err = self._run_git(["commit", "-m", commit_msg])
        if code != 0:
            # Check if nothing changed
            if "nothing to commit" in c_out.lower() or "nothing to commit" in c_err.lower():
                self._update_status(status="OK", result="UP_TO_DATE")
                return True
            logger.warning(f"git commit warning: {c_err}")

        # 7. Push to remote
        logger.info(f"Pushing telemetry commit to {self.remote_name} {self.branch_name}...")
        code, p_out, p_err = self._run_git(["push", self.remote_name, self.branch_name], timeout_sec=60)
        if code != 0:
            logger.error(f"git push failed: {p_err or p_out}")
            self._update_status(status="DEGRADED", result=f"PUSH_FAILED: {(p_err or p_out)[:150]}")
            return False

        # Push Succeeded
        self.last_push_at = now_dt.isoformat()
        self.last_push_result = "SUCCESS"
        self.git_sync_status = "OK"
        logger.info(f"✅ Git telemetry push succeeded ({commit_msg})")
        self._update_status(status="OK", last_push_at=self.last_push_at, result="SUCCESS")
        return True

    def _update_status(self, status: str, last_push_at: Optional[str] = None, result: Optional[str] = None):
        self.git_sync_status = status
        if last_push_at:
            self.last_push_at = last_push_at
        if result:
            self.last_push_result = result

        if self.exporter:
            try:
                self.exporter.update_git_status(
                    status=self.git_sync_status,
                    last_push_at=self.last_push_at,
                    result=self.last_push_result,
                )
            except Exception:
                pass

    def _run_git(self, args: List[str], timeout_sec: int = 30) -> Tuple[int, str, str]:
        """Runs a git command safely with timeout."""
        try:
            cmd = ["git"] + args
            proc = subprocess.run(
                cmd,
                cwd=self.repo_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Git command timed out ({timeout_sec}s): {' '.join(args)}"
        except Exception as e:
            return -1, "", str(e)
