"""Console entry point: ``devops``."""
import sys


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-V"):
        from auto_devops_agent import __version__
        print(f"autonomous-devops-agent {__version__}")
        return
    if args and args[0] in ("--help", "-h"):
        print(
            "Usage: devops [--version] [--doctor]\n\n"
            "Run `devops` inside the project folder you want to deploy.\n"
            "  --doctor   check Ollama / Qdrant / Docker / git / API keys"
        )
        return

    from auto_devops_agent import _bootstrap
    _bootstrap.setup()

    if args and args[0] == "--doctor":
        from auto_devops_agent.doctor import run
        sys.exit(run())

    import devops  # devops_agent/devops.py
    devops.main()


if __name__ == "__main__":
    main()
