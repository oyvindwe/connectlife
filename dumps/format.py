import json
from collections import defaultdict
from os import listdir
from os.path import isfile, join

import numpy as np
import pandas as pd

files = list(filter(lambda f: f[-5:] == ".json", [f for f in listdir(".") if isfile(join(".", f))]))

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

    for device_type in sorted(all_appliances):
        appliances = all_appliances[device_type]
        f.write(f"- [{device_type}]({device_type}.md): {appliances.loc['deviceNickName'].values[0]}\n")

        appliances.replace({np.nan: None}, inplace=True)
        appliances.sort_index(inplace=True)

        with open(f"{device_type}.md", "w") as a:
            a.write(f"# {device_type}\n\n")
            a.write(appliances.to_markdown())
            a.write("\n\n")
            a.write("## Generated from\n\n")
            for filename in all_filenames[device_type]:
                a.write(f"- [`{device_type}`]({filename})\n")
