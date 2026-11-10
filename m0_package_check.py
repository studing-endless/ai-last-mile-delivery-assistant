
import json
from pathlib import Path

data_dir = Path(__file__).parent / "data" / "raw"

# 1. 读取路线数据
with open(data_dir / "route_data.json", encoding="utf-8") as f:
    routes = json.load(f)

route_id = next(iter(routes))
route = routes[route_id]


# 2. 从大型JSON文件中提取指定路线
def load_one_route(file_path, target_route_id):
    target = json.dumps(target_route_id)
    decoder = json.JSONDecoder()
    block_size = 64 * 1024

    with open(file_path, "r", encoding="utf-8") as f:
        buffer = ""

        # 找到目标路线ID
        while True:
            chunk = f.read(block_size)
            if not chunk:
                raise KeyError(f"找不到路线：{target_route_id}")

            buffer += chunk
            pos = buffer.find(target)

            if pos >= 0:
                buffer = buffer[pos + len(target):]
                break

            buffer = buffer[-len(target):]

        # 找到路线ID后面的数据
        while ":" not in buffer:
            chunk = f.read(block_size)
            if not chunk:
                raise ValueError("路线数据格式不完整")
            buffer += chunk

        buffer = buffer.split(":", 1)[1].lstrip()

        # 使用Python自带的JSON解析器
        while True:
            try:
                result, _ = decoder.raw_decode(buffer)
                return result

            except json.JSONDecodeError as exc:
                chunk = f.read(block_size)
                if not chunk:
                    raise ValueError("路线数据解析失败") from exc
                buffer += chunk


# 3. 获取第一条路线的包裹数据
packages = load_one_route(
    data_dir / "package_data.json",
    route_id
)

# 4. 统计结果
package_count = sum(len(p) for p in packages.values())

print("路线ID：", route_id)
print("路线站点数：", len(route["stops"]))
print("包裹数据涉及站点数：", len(packages))
print("包裹记录总数：", package_count)

unknown_stops = set(packages) - set(route["stops"])
print("不存在的站点：", unknown_stops)

# 5. 展示一个真实包裹
for stop_id, stop_packages in packages.items():
    if stop_packages:
        package_id, info = next(iter(stop_packages.items()))

        print("\n示例站点：", stop_id)
        print("示例包裹ID：", package_id)
        print("包裹详细信息：", info)
        break
#这276条包裹记录，有多少条具备完整时间窗口？它们的计划服务时间是多少？

import math
from collections import Counter

status_count = Counter()
valid_window_count = 0
partial_window_count = 0
service_seconds = 0.0
invalid_service_count = 0

for stop_id, stop_packages in packages.items():
    for package_id, info in stop_packages.items():

        # 统计包裹扫描状态
        status_count[info.get("scan_status", "UNKNOWN")] += 1

        # 检查计划服务时间
        service = info.get("planned_service_time_seconds")

        if isinstance(service, (int, float)) and math.isfinite(service) and service >= 0:
            service_seconds += service
        else:
            invalid_service_count += 1

        # 检查配送时间窗口
        window = info.get("time_window") or {}
        start = window.get("start_time_utc")
        end = window.get("end_time_utc")

        has_start = isinstance(start, str) and bool(start.strip())
        has_end = isinstance(end, str) and bool(end.strip())

        if has_start and has_end:
            valid_window_count += 1
        elif has_start or has_end:
            partial_window_count += 1

# 查看配送站是否有包裹
station_ids = [
    sid for sid, stop in route["stops"].items()
    if stop["type"] == "Station"
]

print("\n===== 包裹数据验收 =====")
print("包裹扫描状态统计：", status_count)
print("双端时间窗口非空的包裹数：", valid_window_count)
print("仅有一端时间信息的包裹数：", partial_window_count)
print("计划服务时间异常或缺失数：", invalid_service_count)
print("包裹级计划服务时间简单合计：", round(service_seconds / 60, 2), "分钟")

for sid in station_ids:
    print("配送站", sid, "包裹记录数：", len(packages.get(sid, {})))

from datetime import datetime

valid_windows = []
invalid_windows = []
window_stop_ids = set()

for stop_id, stop_packages in packages.items():
    for package_id, info in stop_packages.items():
        window = info.get("time_window") or {}

        start = window.get("start_time_utc")
        end = window.get("end_time_utc")

        # 只检查有完整时间窗口的包裹
        if not (isinstance(start, str) and isinstance(end, str)):
            continue

        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))

            if start_dt >= end_dt:
                invalid_windows.append(package_id)
            else:
                valid_windows.append(package_id)
                window_stop_ids.add(stop_id)

        except ValueError:
            invalid_windows.append(package_id)

print("\n===== 时间窗口有效性检查 =====")
print("有效时间窗口包裹数：", len(valid_windows))
print("无效时间窗口包裹数：", len(invalid_windows))
print("涉及的不同站点数：", len(window_stop_ids))
