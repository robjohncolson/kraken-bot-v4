"""Launch the TUI operator cockpit: ``python -m tui``"""

from tui.app import KrakenCockpit


def main() -> None:
    KrakenCockpit().run()


if __name__ == "__main__":
    main()
