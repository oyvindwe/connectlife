import json
from collections import defaultdict

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
    "HWFS1015AB.json",
    "hisenserefrigeratordump.json",
]

all_appliances = defaultdict(pd.DataFrame)
all_filenames = defaultdict(list)
for filename in files:
    with (open(filename) as f):
        appliance = pd.json_normalize(json.load(f))
        appliance = appliance.transpose()
        appliance.columns = [filename[:-5].replace("_", " ")]
        device_type = appliance.loc["deviceTypeCode"].iloc[0]
        all_appliances[device_type] = pd.concat([all_appliances[device_type], appliance], axis=1)
        all_filenames[device_type].append(filename)

with open("README.md", "w") as f:
    f.write("# Appliance dumps\n\n")

    for device_type, appliances in all_appliances.items():
        f.write(f"- [{device_type}]({device_type}.md)\n")

        appliances.replace({np.nan: None}, inplace=True)
        appliances.sort_index(inplace=True)

        with open(f"{device_type}.md", "w") as a:
            a.write(f"# {device_type}\n\n")
            a.write(appliances.to_markdown())
            a.write("\n\n")
            a.write("## Generated from\n\n")
            for filename in all_filenames[device_type]:
                a.write(f"- [`{filename}`]({filename})\n")
