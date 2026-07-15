"""Constants for Xiaomi Cloud integration."""
CONF_WAKE_ON_START = "enable_wake_on_start"
DOMAIN = "xiaomi_cloud"

# ── 后端代理配置 ──
CONF_ENDPOINT = "endpoint"
DEFAULT_ENDPOINT = "http://localhost:8080"

# ── FastRetry（后端登录中时的快速重试） ──
FAST_RETRY_MAX = 12
FAST_RETRY_DEFAULT_SECONDS = 10
COORDINATOR = "coordinator"
DATA_LISTENER = "listener"
UNDO_UPDATE_LISTENER = "undo_update_listener"
DEFAULT_SCAN_INTERVAL = 660
DEFAULT_WAKE_ON_START = False
MIN_SCAN_INTERVAL = 60
SIGNAL_STATE_UPDATED = f"{DOMAIN}.updated"
CONF_COORDINATE_TYPE = "coordinate_type"
CONF_COORDINATE_TYPE_BAIDU = "baidu"
CONF_COORDINATE_TYPE_ORIGINAL = "original"
CONF_COORDINATE_TYPE_GOOGLE = "google"
CONF_GAODE_APIKEY = "gaode_api_key"
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 5  # 默认每5分钟更新一次
CONF_LOW_BATTERY_POLLING = "enable_low_battery_polling"  # 低电量时保持默认更新频率（勾选=不加速）
DEFAULT_LOW_BATTERY_POLLING = False  # 默认不勾选 → 低电量时自动加速
CONF_LOW_BATTERY_THRESHOLD = "low_battery_threshold"  # 低电量阈值
DEFAULT_LOW_BATTERY_THRESHOLD = 40  # 默认40%为低电量
CONF_LOW_BATTERY_INTERVAL = "low_battery_interval"  # 低电量时的更新间隔
DEFAULT_LOW_BATTERY_INTERVAL = 3  # 默认低电量时3分钟更新一次（快于正常5分钟）
CONF_ENABLE_GAODE_MORE_INFO = "enable_gaode_more_info"  # 在设备详情页显示高德地图
DEFAULT_ENABLE_GAODE_MORE_INFO = True   # 默认启用（安装 gaode_maps 后自动生效）
CONF_ACTIVE_LOCATE = "enable_active_locate"  # 主动定位（触发小米云实时定位）
DEFAULT_ACTIVE_LOCATE = True

