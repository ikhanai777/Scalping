from .cli import main

# The guard matters on Windows: worker processes (the startup bootstrap backtest) are spawned by
# re-importing __main__, and an unguarded main() would start a second server in the child.
if __name__ == "__main__":
    main()
