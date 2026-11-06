"""Cron wrapper: Data Lab slot matchup."""
import runpy, sys
sys.argv = ["data_lab_slot.py", "matchup"]
runpy.run_path(__file__.rsplit("/", 1)[0] + "/data_lab_slot.py", run_name="__main__")
