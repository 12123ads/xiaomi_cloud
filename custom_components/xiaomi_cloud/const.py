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

# ── 基于移动速度的三档轮询 ──
MOVEMENT_FAST_INTERVAL = 1
MOVEMENT_MEDIUM_INTERVAL = 5
MOVEMENT_SLOW_INTERVAL = 10
MOVEMENT_MEDIUM_SPEED_KMH = 1.0
MOVEMENT_FAST_SPEED_KMH = 10.0
CONF_ENABLE_GAODE_MORE_INFO = "enable_gaode_more_info"  # 在设备详情页显示高德地图
DEFAULT_ENABLE_GAODE_MORE_INFO = True   # 默认启用（安装 gaode_maps 后自动生效）
CONF_ACTIVE_LOCATE = "enable_active_locate"  # 主动定位（触发小米云实时定位）
DEFAULT_ACTIVE_LOCATE = True

