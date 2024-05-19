import json

import numpy as np
import pandas as pd

files = [
    "Asko_dishwasher.json",
    "Gorenje_dishwasher.json",
    "Asko_hood_1.json",
    "Asko_hood_2.json",
    "Asko_induction_hob_1.json",
    "Asko_induction_hob_2.json",
    "Oven_type_1.json",
    "Oven_type_2.json",
    "Asko_professional_washing_machine.json",
    "Gorenje_washing_machine.json",
    "Asko_professional_tumble_dryer.json",
    "Heat_pump.json",
    "airCondDumpHisense.json",
]

appliances = pd.DataFrame()
for filename in files:
    with open(filename) as f:
        appliance = pd.json_normalize(json.load(f)).transpose()
        appliance.columns = [filename[:-5].replace("_", " ")]
        appliances = pd.concat([appliances, appliance], axis=1)

appliances.replace({np.nan: None}, inplace=True)
appliances.sort_index(inplace=True)

with open("README.md", "w") as f:
    f.write("# Appliance dumps\n\n")
    f.write(appliances.to_markdown())
    f.write("\n\n")
    f.write("## Generated from\n\n")
    for filename in files:
        f.write(f"- [{filename[:-5]}]({filename})\n")


