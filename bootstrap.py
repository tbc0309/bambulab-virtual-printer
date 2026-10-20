"""Check Python and install missing runtime dependencies before starting the lab."""

import importlib.metadata
import argparse
import pathlib
import ssl
import subprocess
import sys
import threading
import traceback

from diagnostics import LogStore


def dependencies_ready():
    try:
        version = importlib.metadata.version("cryptography")
        if not 44 <= int(version.split(".")[0]) < 47:
            return False
        import cryptography.x509  # Verify the installed package actually loads.

        return hasattr(cryptography.x509, "Certificate")
    except (ImportError, ValueError):
        return False


def main():
    if sys.version_info < (3, 10):
        raise SystemExit(
            "Python 3.10+ is required. Install a supported Python and run start again."
        )
    root = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", type=pathlib.Path, default=root / "data")
    args, _ = parser.parse_known_args()
    logs = LogStore(args.data_dir)
    logs.record(
        "runtime",
        "startup-check",
        {"python": sys.version.split()[0], "tls": ssl.OPENSSL_VERSION},
    )
    print(f"Python {sys.version.split()[0]} | {ssl.OPENSSL_VERSION}", flush=True)
    for name in (
        "catalog.json",
        "VERSION",
        "web/index.html",
        "web/i18n.js",
        "web/activity.js",
        "web/locales.js",
        "assets/camera/test.h264",
        "assets/camera/test.jpg",
    ):
        if not (root / name).is_file():
            logs.record("runtime", "missing-resource", {"file": name}, level="error")
            raise SystemExit(
                f"Missing project file: {name}. Download the complete project."
            )
    if not dependencies_ready():
        print("Installing runtime dependencies from requirements.txt...", flush=True)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(root / "requirements.txt"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            logs.record(
                "runtime", "dependencies-installed", {"output": result.stdout[-4096:]}
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            logs.record(
                "runtime",
                "dependency-install-failed",
                {
                    "type": type(exc).__name__,
                    "output": str(getattr(exc, "stderr", "") or "")[-4096:],
                },
                level="error",
            )
            logs.catalogue()
            raise SystemExit(
                "Dependency installation failed. Check network access and pip, then run start again."
            )
        if not dependencies_ready():
            raise SystemExit(
                "Runtime dependency check failed. Recreate .venv and run start again."
            )
    previous_hook = threading.excepthook

    def thread_error(args):
        logs.record(
            "runtime",
            "thread-error",
            {
                "thread": args.thread.name,
                "type": args.exc_type.__name__,
                "traceback": "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                ),
            },
            level="error",
        )

    threading.excepthook = thread_error
    try:
        from lab import main as start

        start(logs=logs)
    except KeyboardInterrupt:
        print("Simulator stopped.", flush=True)
    except Exception as exc:
        logs.record(
            "runtime",
            "startup-failed",
            {"type": type(exc).__name__, "traceback": traceback.format_exc()},
            level="error",
        )
        raise SystemExit(
            f"Startup failed: {exc}. Check ports, data-directory permissions and project files."
        ) from exc
    finally:
        threading.excepthook = previous_hook
        logs.catalogue()


if __name__ == "__main__":
    main()
