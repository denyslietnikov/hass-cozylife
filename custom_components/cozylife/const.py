DOMAIN = "cozylife"

# http://doc.doit/project-5/doc-8/
SWITCH_TYPE_CODE = "00"
LIGHT_TYPE_CODE = "01"
SUPPORT_DEVICE_CATEGORY = [SWITCH_TYPE_CODE, LIGHT_TYPE_CODE]

CONF_DEVICE_TYPE_CODE = "device_type_code"
CONF_SUBNET = "subnet"
CONF_DEVICES = "devices"
CONF_SWITCH_INTERVAL = "switch_interval"
CONF_LIGHT_INTERVAL = "light_interval"
CONF_DEFAULT_TRANSITION = "default_transition"
DEFAULT_SWITCH_INTERVAL = 5
DEFAULT_LIGHT_INTERVAL = 60
DEFAULT_TRANSITION_SECONDS = 2
MAX_DEFAULT_TRANSITION_SECONDS = 60
SCENES = ["manual", "natural", "sleep", "warm", "study", "chrismas"]

PLATFORMS = ["light", "switch", "select", "number"]

PLATFORMS_BY_TYPE = {
    LIGHT_TYPE_CODE: "light",
    SWITCH_TYPE_CODE: "switch",
}

# http://doc.doit/project-5/doc-8/
SWITCH = "1"
WORK_MODE = "2"
TEMP = "3"
BRIGHT = "4"
HUE = "5"
SAT = "6"
LIGHT_COUNTDOWN = "13"

LIGHT_DPID = [SWITCH, WORK_MODE, TEMP, BRIGHT, HUE, SAT]
SWITCH_DPID = [
    SWITCH,
]

DEFAULT_MIN_KELVIN = 2700
DEFAULT_MAX_KELVIN = 6500
