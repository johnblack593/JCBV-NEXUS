import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexus"))

from scripts.auto_calibrate import WFOAutoCalibrator

def main():
    print("Iniciando mini calibración WFO (solo BINARY)...")
    calibrator = WFOAutoCalibrator()
    # No sobrescribimos la BINARY_GRID, dejamos que evalúe 1,2,3,4,5
    calibrator.run_full_calibration(mode="binary")

if __name__ == "__main__":
    main()
