"""Pretends to run a Maestro flow: takes ~1.5s, fails if the flow file name contains 'fail'."""
import sys
import time

time.sleep(1.5)
flow = next((a for a in sys.argv if a.endswith(".yaml")), "")
print(f"Running {flow} with {sys.argv[1:]}")
sys.exit(1 if "fail" in flow else 0)
