import json

CONFIG_FILE = "config.json"

def read_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def write_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)
