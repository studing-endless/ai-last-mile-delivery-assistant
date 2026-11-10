import json
from pathlib import Path

# 找到下载的数据文件
path = Path(__file__).parent / "data/raw/route_data.json"

# 读取真实 JSON 数据 将 Amazon 的 JSON 文件读取为 Python 数据。
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

# 查看数据基本情况
print("路线总数：", len(data))

route_id = next(iter(data))
route = data[route_id]

print("第一条路线ID：", route_id)
#查看这条路线包含哪些业务字段
print("包含的字段：", list(route.keys()))
print("配送站点数：", len(route.get("stops", {})))


# 查看路线基本信息
print("\n配送站：", route["station_code"])
print("配送日期：", route["date_YYYY_MM_DD"])
print("出发时间：", route["departure_time_utc"])

# 查看第一个站点的详细信息
stop_id = next(iter(route["stops"]))
stop = route["stops"][stop_id]

print("\n第一个站点ID：", stop_id)
print("站点详细信息：", stop)

from collections import Counter

# 统计不同类型的站点数量
types = [s["type"] for s in route["stops"].values()]

print("\n站点类型统计：", Counter(types))


# 读取司机历史配送顺序
sequence_path = path.parent / "actual_sequences.json"

with open(sequence_path, "r", encoding="utf-8") as f:
    sequences = json.load(f)

# 根据之前的路线ID，找到对应的历史顺序
sequence_info = sequences[route_id]

print("\n历史顺序数据：", sequence_info)



# 获取真实访问顺序
actual = sequences[route_id]["actual"]

# 按顺序编号排序
ordered_stops = sorted(actual, key=actual.get)

# 检查数据是否完整
print("\n顺序记录数：", len(actual))
print("站点完全匹配：", set(actual) == set(route["stops"]))
print("顺序编号连续：", sorted(actual.values()) == list(range(len(actual))))

# 查看实际访问的前10站
print("\n实际访问的前10个站点：")
for i, stop_id in enumerate(ordered_stops[:10]):
    stop_type = route["stops"][stop_id]["type"]
    print(i, stop_id, stop_type)
    
    
import ijson

# 交通时间文件
travel_path = path.parent / "travel_times.json"

# 只提取第一条路线的交通时间矩阵
matrix = None

with open(travel_path, "rb") as f:
    for rid, times in ijson.kvitems(f, ""):
        if rid == route_id:
            matrix = times
            break

if matrix is None:
    raise ValueError("没有找到对应路线的交通时间数据")

# 检查站点是否匹配
assert set(matrix) == set(route["stops"])

# 按司机历史顺序累计预计行驶时间
total_seconds = 0

for a, b in zip(ordered_stops[:-1], ordered_stops[1:]):
    total_seconds += float(matrix[a][b])

print("\n历史路线预计行驶时间：")
print(f"{total_seconds / 60:.2f} 分钟")
print(f"{total_seconds / 3600:.2f} 小时")

# 单独计算从最后一个投递点返回配送站的时间
return_seconds = float(matrix[ordered_stops[-1]][ordered_stops[0]])

print(f"预计返回配送站时间：{return_seconds / 60:.2f} 分钟")
print(f"含返回行程的预计行驶时间：{(total_seconds + return_seconds) / 60:.2f} 分钟")