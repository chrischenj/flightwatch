"""城市 / 枢纽 / 坐标表。

用途:
  * 把用户输入（中文名、英文名、三字码）解析成统一的城市代码
  * 给出候选枢纽（用于「在哪儿拆票」）
  * 距离 / 时差粗算（用于休息价值评分）

说明: 时区偏移取「标准时」，不含夏令时；只用于打分启发式，不用于精确计算。
Kiwi 的城市 ID 能在运行时用 places 查询自动解析，这里已知的写上以减少一次请求。
"""
from __future__ import annotations

import math
import unicodedata
from typing import Optional

# code, 中文名, 英文名, lat, lon, 标准时区偏移, kiwi 城市 ID
_RAW = [
    # ---------- 中国 ----------
    ("BJS", "北京", "Beijing", 39.9042, 116.4074, 8.0, "City:beijing_cn"),
    ("SHA", "上海", "Shanghai", 31.2304, 121.4737, 8.0, "City:shanghai_cn"),
    ("CAN", "广州", "Guangzhou", 23.1291, 113.2644, 8.0, None),
    ("SZX", "深圳", "Shenzhen", 22.5431, 114.0579, 8.0, None),
    ("CTU", "成都", "Chengdu", 30.5728, 104.0668, 8.0, None),
    ("CKG", "重庆", "Chongqing", 29.5630, 106.5516, 8.0, None),
    ("HGH", "杭州", "Hangzhou", 30.2741, 120.1551, 8.0, None),
    ("NKG", "南京", "Nanjing", 32.0603, 118.7969, 8.0, None),
    ("WUH", "武汉", "Wuhan", 30.5928, 114.3055, 8.0, None),
    ("XIY", "西安", "Xi'an", 34.3416, 108.9398, 8.0, None),
    ("KMG", "昆明", "Kunming", 25.0389, 102.7183, 8.0, None),
    ("XMN", "厦门", "Xiamen", 24.4798, 118.0894, 8.0, None),
    ("TAO", "青岛", "Qingdao", 36.0671, 120.3826, 8.0, None),
    ("TSN", "天津", "Tianjin", 39.0842, 117.2009, 8.0, None),
    ("CSX", "长沙", "Changsha", 28.2282, 112.9388, 8.0, None),
    ("CGO", "郑州", "Zhengzhou", 34.7466, 113.6254, 8.0, None),
    ("HRB", "哈尔滨", "Harbin", 45.8038, 126.5350, 8.0, None),
    ("SHE", "沈阳", "Shenyang", 41.8057, 123.4315, 8.0, None),
    ("URC", "乌鲁木齐", "Urumqi", 43.8256, 87.6168, 8.0, None),
    ("HAK", "海口", "Haikou", 20.0440, 110.1999, 8.0, None),
    ("SYX", "三亚", "Sanya", 18.2528, 109.5119, 8.0, None),
    ("FOC", "福州", "Fuzhou", 26.0745, 119.2965, 8.0, None),
    ("NNG", "南宁", "Nanning", 22.8170, 108.3665, 8.0, None),
    ("HKG", "香港", "Hong Kong", 22.3193, 114.1694, 8.0, "City:hong-kong_hk"),
    ("TPE", "台北", "Taipei", 25.0330, 121.5654, 8.0, "City:taipei_tw"),
    ("MFM", "澳门", "Macau", 22.1987, 113.5439, 8.0, None),
    # ---------- 亚洲枢纽 ----------
    ("IST", "伊斯坦布尔", "Istanbul", 41.0082, 28.9784, 3.0, "City:istanbul_tr"),
    ("DOH", "多哈", "Doha", 25.2854, 51.5310, 3.0, "City:doha_qa"),
    ("DXB", "迪拜", "Dubai", 25.2048, 55.2708, 4.0, "City:dubai_ae"),
    ("AUH", "阿布扎比", "Abu Dhabi", 24.4539, 54.3773, 4.0, "City:abu-dhabi_ae"),
    ("RUH", "利雅得", "Riyadh", 24.7136, 46.6753, 3.0, "City:riyadh_sa"),
    ("TLV", "特拉维夫", "Tel Aviv", 32.0853, 34.7818, 2.0, "City:tel-aviv_il"),
    ("DEL", "德里", "Delhi", 28.6139, 77.2090, 5.5, "City:delhi_in"),
    ("BOM", "孟买", "Mumbai", 19.0760, 72.8777, 5.5, "City:mumbai_in"),
    ("SIN", "新加坡", "Singapore", 1.3521, 103.8198, 8.0, "City:singapore_sg"),
    ("KUL", "吉隆坡", "Kuala Lumpur", 3.1390, 101.6869, 8.0, "City:kuala-lumpur_my"),
    ("BKK", "曼谷", "Bangkok", 13.7563, 100.5018, 7.0, "City:bangkok_th"),
    ("CGK", "雅加达", "Jakarta", -6.2088, 106.8456, 7.0, "City:jakarta_id"),
    ("MNL", "马尼拉", "Manila", 14.5995, 120.9842, 8.0, "City:manila_ph"),
    ("ICN", "首尔", "Seoul", 37.5665, 126.9780, 9.0, "City:seoul_kr"),
    ("NRT", "东京", "Tokyo", 35.6762, 139.6503, 9.0, "City:tokyo_jp"),
    ("OSA", "大阪", "Osaka", 34.6937, 135.5023, 9.0, "City:osaka_jp"),
    ("TAS", "塔什干", "Tashkent", 41.2995, 69.2401, 5.0, "City:tashkent_uz"),
    ("ALA", "阿拉木图", "Almaty", 43.2389, 76.8897, 5.0, "City:almaty_kz"),
    # ---------- 欧洲枢纽 ----------
    ("LHR", "伦敦", "London", 51.5074, -0.1278, 0.0, "City:london_gb"),
    ("AMS", "阿姆斯特丹", "Amsterdam", 52.3676, 4.9041, 1.0, "City:amsterdam_nl"),
    ("FRA", "法兰克福", "Frankfurt", 50.1109, 8.6821, 1.0, "City:frankfurt_de"),
    ("MUC", "慕尼黑", "Munich", 48.1351, 11.5820, 1.0, "City:munich_de"),
    ("CDG", "巴黎", "Paris", 48.8566, 2.3522, 1.0, "City:paris_fr"),
    ("FCO", "罗马", "Rome", 41.9028, 12.4964, 1.0, "City:rome_it"),
    ("MAD", "马德里", "Madrid", 40.4168, -3.7038, 1.0, "City:madrid_es"),
    ("ZRH", "苏黎世", "Zurich", 47.3769, 8.5417, 1.0, "City:zurich_ch"),
    ("VIE", "维也纳", "Vienna", 48.2082, 16.3738, 1.0, "City:vienna_at"),
    ("HEL", "赫尔辛基", "Helsinki", 60.1699, 24.9384, 2.0, "City:helsinki_fi"),
    ("CPH", "哥本哈根", "Copenhagen", 55.6761, 12.5683, 1.0, "City:copenhagen_dk"),
    ("LIS", "里斯本", "Lisbon", 38.7223, -9.1393, 0.0, "City:lisbon_pt"),
    ("SVO", "莫斯科", "Moscow", 55.7558, 37.6173, 3.0, "City:moscow_ru"),
    # ---------- 美洲 ----------
    ("JFK", "纽约", "New York", 40.7128, -74.0060, -5.0, "City:new-york-city_ny_us"),
    ("BOS", "波士顿", "Boston", 42.3601, -71.0589, -5.0, "City:boston_ma_us"),
    ("IAD", "华盛顿", "Washington", 38.9072, -77.0369, -5.0, "City:washington_dc_us"),
    ("ORD", "芝加哥", "Chicago", 41.8781, -87.6298, -6.0, "City:chicago_il_us"),
    ("DFW", "达拉斯", "Dallas", 32.7767, -96.7970, -6.0, "City:dallas_tx_us"),
    ("LAX", "洛杉矶", "Los Angeles", 34.0522, -118.2437, -8.0, "City:los-angeles_ca_us"),
    ("SFO", "旧金山", "San Francisco", 37.7749, -122.4194, -8.0, "City:san-francisco_ca_us"),
    ("SEA", "西雅图", "Seattle", 47.6062, -122.3321, -8.0, "City:seattle_wa_us"),
    ("YVR", "温哥华", "Vancouver", 49.2827, -123.1207, -8.0, "City:vancouver_bc_ca"),
    ("YYZ", "多伦多", "Toronto", 43.6532, -79.3832, -5.0, "City:toronto_on_ca"),
    ("MEX", "墨西哥城", "Mexico City", 19.4326, -99.1332, -6.0, "City:mexico-city_mx"),
    ("GRU", "圣保罗", "Sao Paulo", -23.5505, -46.6333, -3.0, "City:sao-paulo_br"),
    ("EZE", "布宜诺斯艾利斯", "Buenos Aires", -34.6037, -58.3816, -3.0, "City:buenos-aires_ar"),
    ("LIM", "利马", "Lima", -12.0464, -77.0428, -5.0, "City:lima_pe"),
    ("SCL", "圣地亚哥", "Santiago", -33.4489, -70.6693, -3.0, "City:santiago_cl"),
    # ---------- 非洲 / 大洋洲 ----------
    ("CAI", "开罗", "Cairo", 30.0444, 31.2357, 2.0, "City:cairo_eg"),
    ("ADD", "亚的斯亚贝巴", "Addis Ababa", 9.0300, 38.7400, 3.0, "City:addis-ababa_et"),
    ("NBO", "内罗毕", "Nairobi", -1.2921, 36.8219, 3.0, "City:nairobi_ke"),
    ("JNB", "约翰内斯堡", "Johannesburg", -26.2041, 28.0473, 2.0, "City:johannesburg_za"),
    ("SYD", "悉尼", "Sydney", -33.8688, 151.2093, 10.0, "City:sydney_nsw_au"),
    ("MEL", "墨尔本", "Melbourne", -37.8136, 144.9631, 10.0, "City:melbourne_vic_au"),
    ("AKL", "奥克兰", "Auckland", -36.8485, 174.7633, 12.0, "City:auckland_nz"),
    # ---------- 扩充：常见旅游/商务目的地 ----------
    ("MIL", "米兰", "Milan", 45.4642, 9.1900, 1.0, "City:milan_it"),
    ("VCE", "威尼斯", "Venice", 45.4408, 12.3155, 1.0, "City:venice_it"),
    ("NAP", "那不勒斯", "Naples", 40.8518, 14.2681, 1.0, "City:naples_it"),
    ("BCN", "巴塞罗那", "Barcelona", 41.3874, 2.1686, 1.0, "City:barcelona_es"),
    ("BER", "柏林", "Berlin", 52.5200, 13.4050, 1.0, "City:berlin_de"),
    ("PRG", "布拉格", "Prague", 50.0755, 14.4378, 1.0, "City:prague_cz"),
    ("BUD", "布达佩斯", "Budapest", 47.4979, 19.0402, 1.0, "City:budapest_hu"),
    ("ATH", "雅典", "Athens", 37.9838, 23.7275, 2.0, "City:athens_gr"),
    ("DUB", "都柏林", "Dublin", 53.3498, -6.2603, 0.0, "City:dublin_ie"),
    ("STO", "斯德哥尔摩", "Stockholm", 59.3293, 18.0686, 1.0, "City:stockholm_se"),
    ("OSL", "奥斯陆", "Oslo", 59.9139, 10.7522, 1.0, "City:oslo_no"),
    ("WAW", "华沙", "Warsaw", 52.2297, 21.0122, 1.0, "City:warsaw_pl"),
    ("GVA", "日内瓦", "Geneva", 46.2044, 6.1432, 1.0, "City:geneva_ch"),
    ("BRU", "布鲁塞尔", "Brussels", 50.8503, 4.3517, 1.0, "City:brussels_be"),
    ("MAN", "曼彻斯特", "Manchester", 53.4808, -2.2426, 0.0, "City:manchester_gb"),
    ("EDI", "爱丁堡", "Edinburgh", 55.9533, -3.1883, 0.0, "City:edinburgh_gb"),
    ("NCE", "尼斯", "Nice", 43.7102, 7.2620, 1.0, "City:nice_fr"),
    ("HAM", "汉堡", "Hamburg", 53.5511, 9.9937, 1.0, "City:hamburg_de"),
    ("DUS", "杜塞尔多夫", "Dusseldorf", 51.2277, 6.7735, 1.0, "City:dusseldorf_de"),
    ("MIA", "迈阿密", "Miami", 25.7617, -80.1918, -5.0, "City:miami_fl_us"),
    ("LAS", "拉斯维加斯", "Las Vegas", 36.1699, -115.1398, -8.0, "City:las-vegas_nv_us"),
    ("HOU", "休斯顿", "Houston", 29.7604, -95.3698, -6.0, "City:houston_tx_us"),
    ("ATL", "亚特兰大", "Atlanta", 33.7490, -84.3880, -5.0, "City:atlanta_ga_us"),
    ("DEN", "丹佛", "Denver", 39.7392, -104.9903, -7.0, "City:denver_co_us"),
    ("HNL", "檀香山", "Honolulu", 21.3069, -157.8583, -10.0, "City:honolulu_hi_us"),
    ("HAN", "河内", "Hanoi", 21.0278, 105.8342, 7.0, "City:hanoi_vn"),
    ("SGN", "胡志明市", "Ho Chi Minh City", 10.8231, 106.6297, 7.0, "City:ho-chi-minh-city_vn"),
    ("DPS", "巴厘岛", "Bali", -8.4095, 115.1889, 8.0, "City:bali_id"),
    ("HKT", "普吉岛", "Phuket", 7.8804, 98.3923, 7.0, "City:phuket_th"),
    ("NGO", "名古屋", "Nagoya", 35.1815, 136.9066, 9.0, "City:nagoya_jp"),
    ("FUK", "福冈", "Fukuoka", 33.5904, 130.4017, 9.0, "City:fukuoka_jp"),
    ("CTS", "札幌", "Sapporo", 43.0618, 141.3545, 9.0, "City:sapporo_jp"),
    ("CMB", "科伦坡", "Colombo", 6.9271, 79.8612, 5.5, "City:colombo_lk"),
    ("KTM", "加德满都", "Kathmandu", 27.7172, 85.3240, 5.75, "City:kathmandu_np"),
    ("JED", "吉达", "Jeddah", 21.4858, 39.1925, 3.0, "City:jeddah_sa"),
    ("MCT", "马斯喀特", "Muscat", 23.5880, 58.3829, 4.0, "City:muscat_om"),
    ("KWI", "科威特", "Kuwait City", 29.3759, 47.9774, 3.0, "City:kuwait-city_kw"),
    ("BAK", "巴库", "Baku", 40.4093, 49.8671, 4.0, "City:baku_az"),
    ("TBS", "第比利斯", "Tbilisi", 41.7151, 44.8271, 4.0, "City:tbilisi_ge"),
    ("CAS", "卡萨布兰卡", "Casablanca", 33.5731, -7.5898, 1.0, "City:casablanca_ma"),
    ("RAK", "马拉喀什", "Marrakesh", 31.6295, -7.9811, 1.0, "City:marrakesh_ma"),
    ("CPT", "开普敦", "Cape Town", -33.9249, 18.4241, 2.0, "City:cape-town_za"),
    ("MRU", "毛里求斯", "Mauritius", -20.3484, 57.5522, 4.0, "City:mauritius_mu"),
    ("BNE", "布里斯班", "Brisbane", -27.4698, 153.0251, 10.0, "City:brisbane_qld_au"),
    ("PER", "珀斯", "Perth", -31.9505, 115.8605, 8.0, "City:perth_wa_au"),
    ("MLE", "马尔代夫", "Maldives", 4.1755, 73.5093, 5.0, "City:male_mv"),
    ("BKI", "亚庇", "Kota Kinabalu", 5.9804, 116.0735, 8.0, "City:kota-kinabalu_my"),
]

# 机场三字码 -> 城市码（同城多机场）
_AIRPORT_TO_CITY = {
    "PEK": "BJS", "PKX": "BJS", "PVG": "SHA", "SHA": "SHA", "CAN": "CAN",
    "SZX": "SZX", "CTU": "CTU", "TFU": "CTU", "CKG": "CKG", "HGH": "HGH",
    "NKG": "NKG", "WUH": "WUH", "XIY": "XIY", "KMG": "KMG", "XMN": "XMN",
    "TAO": "TAO", "TSN": "TSN", "CSX": "CSX", "CGO": "CGO", "HRB": "HRB",
    "SHE": "SHE", "URC": "URC", "HAK": "HAK", "SYX": "SYX", "FOC": "FOC",
    "NNG": "NNG", "HKG": "HKG", "TPE": "TPE", "MFM": "MFM",
    "IST": "IST", "SAW": "IST", "DOH": "DOH", "DXB": "DXB", "AUH": "AUH",
    "RUH": "RUH", "TLV": "TLV", "DEL": "DEL", "BOM": "BOM", "SIN": "SIN",
    "KUL": "KUL", "BKK": "BKK", "DMK": "BKK", "CGK": "CGK", "MNL": "MNL",
    "ICN": "ICN", "GMP": "ICN", "NRT": "NRT", "HND": "NRT", "KIX": "OSA",
    "TAS": "TAS", "ALA": "ALA",
    "LHR": "LHR", "LGW": "LHR", "STN": "LHR", "AMS": "AMS", "FRA": "FRA",
    "MUC": "MUC", "CDG": "CDG", "ORY": "CDG", "FCO": "FCO", "MAD": "MAD",
    "ZRH": "ZRH", "VIE": "VIE", "HEL": "HEL", "CPH": "CPH", "LIS": "LIS",
    "SVO": "SVO", "DME": "SVO",
    "JFK": "JFK", "EWR": "JFK", "LGA": "JFK", "BOS": "BOS", "IAD": "IAD",
    "DCA": "IAD", "ORD": "ORD", "DFW": "DFW", "LAX": "LAX", "SFO": "SFO",
    "SEA": "SEA", "YVR": "YVR", "YYZ": "YYZ", "MEX": "MEX", "GRU": "GRU",
    "EZE": "EZE", "LIM": "LIM", "SCL": "SCL",
    "CAI": "CAI", "ADD": "ADD", "NBO": "NBO", "JNB": "JNB",
    "SYD": "SYD", "MEL": "MEL", "AKL": "AKL",
    # 扩充
    "MXP": "MIL", "LIN": "MIL", "BGY": "MIL", "VCE": "VCE", "NAP": "NAP",
    "BCN": "BCN", "BER": "BER", "PRG": "PRG", "BUD": "BUD", "ATH": "ATH",
    "DUB": "DUB", "ARN": "STO", "OSL": "OSL", "WAW": "WAW", "GVA": "GVA",
    "BRU": "BRU", "MAN": "MAN", "EDI": "EDI", "NCE": "NCE", "HAM": "HAM",
    "DUS": "DUS", "MIA": "MIA", "LAS": "LAS", "IAH": "HOU", "ATL": "ATL",
    "DEN": "DEN", "HNL": "HNL", "HAN": "HAN", "SGN": "SGN", "DPS": "DPS",
    "HKT": "HKT", "NGO": "NGO", "FUK": "FUK", "CTS": "CTS", "CMB": "CMB",
    "KTM": "KTM", "JED": "JED", "MCT": "MCT", "KWI": "KWI", "GYD": "BAK",
    "TBS": "TBS", "CMN": "CAS", "RAK": "RAK", "CPT": "CPT", "MRU": "MRU",
    "BNE": "BNE", "PER": "PER", "MLE": "MLE", "BKI": "BKI",
}

# 中文别名
_ALIAS = {
    "北京": "BJS", "上海": "SHA", "广州": "CAN", "深圳": "SZX", "成都": "CTU",
    "重庆": "CKG", "杭州": "HGH", "南京": "NKG", "武汉": "WUH", "西安": "XIY",
    "昆明": "KMG", "厦门": "XMN", "青岛": "TAO", "天津": "TSN", "长沙": "CSX",
    "郑州": "CGO", "哈尔滨": "HRB", "沈阳": "SHE", "乌鲁木齐": "URC",
    "海口": "HAK", "三亚": "SYX", "福州": "FOC", "南宁": "NNG", "香港": "HKG",
    "台北": "TPE", "澳门": "MFM", "澳门特别行政区": "MFM",
    "纽约": "JFK", "洛杉矶": "LAX", "旧金山": "SFO", "西雅图": "SEA",
    "芝加哥": "ORD", "波士顿": "BOS", "华盛顿": "IAD", "达拉斯": "DFW",
    "温哥华": "YVR", "多伦多": "YYZ", "墨西哥城": "MEX", "圣保罗": "GRU",
    "布宜诺斯艾利斯": "EZE", "利马": "LIM", "圣地亚哥": "SCL",
    "伦敦": "LHR", "巴黎": "CDG", "法兰克福": "FRA", "慕尼黑": "MUC",
    "阿姆斯特丹": "AMS", "罗马": "FCO", "马德里": "MAD", "苏黎世": "ZRH",
    "维也纳": "VIE", "赫尔辛基": "HEL", "哥本哈根": "CPH", "里斯本": "LIS",
    "莫斯科": "SVO", "伊斯坦布尔": "IST", "多哈": "DOH", "迪拜": "DXB",
    "阿布扎比": "AUH", "利雅得": "RUH", "特拉维夫": "TLV", "德里": "DEL",
    "新德里": "DEL", "孟买": "BOM", "新加坡": "SIN", "吉隆坡": "KUL",
    "曼谷": "BKK", "雅加达": "CGK", "马尼拉": "MNL", "首尔": "ICN",
    "东京": "NRT", "大阪": "OSA", "塔什干": "TAS", "阿拉木图": "ALA",
    "开罗": "CAI", "亚的斯亚贝巴": "ADD", "内罗毕": "NBO",
    "约翰内斯堡": "JNB", "悉尼": "SYD", "墨尔本": "MEL", "奥克兰": "AKL",
    # 扩充：常见旅游/商务目的地
    "米兰": "MIL", "威尼斯": "VCE", "那不勒斯": "NAP", "巴塞罗那": "BCN",
    "柏林": "BER", "布拉格": "PRG", "布达佩斯": "BUD", "雅典": "ATH",
    "都柏林": "DUB", "斯德哥尔摩": "STO", "奥斯陆": "OSL", "华沙": "WAW",
    "日内瓦": "GVA", "布鲁塞尔": "BRU", "曼彻斯特": "MAN", "爱丁堡": "EDI",
    "尼斯": "NCE", "汉堡": "HAM", "杜塞尔多夫": "DUS", "迈阿密": "MIA",
    "拉斯维加斯": "LAS", "休斯顿": "HOU", "亚特兰大": "ATL", "丹佛": "DEN",
    "檀香山": "HNL", "河内": "HAN", "胡志明市": "SGN", "胡志明": "SGN",
    "巴厘岛": "DPS", "普吉岛": "HKT", "普吉": "HKT", "名古屋": "NGO",
    "福冈": "FUK", "札幌": "CTS", "科伦坡": "CMB", "加德满都": "KTM",
    "吉达": "JED", "马斯喀特": "MCT", "科威特": "KWI", "巴库": "BAK",
    "第比利斯": "TBS", "卡萨布兰卡": "CAS", "马拉喀什": "RAK",
    "开普敦": "CPT", "毛里求斯": "MRU", "布里斯班": "BNE", "珀斯": "PER",
    "马尔代夫": "MLE", "马累": "MLE", "亚庇": "BKI", "沙巴": "BKI",
}

CITIES: dict[str, dict] = {
    code: {
        "code": code,
        "zh": zh,
        "en": en,
        "lat": lat,
        "lon": lon,
        "tz": tz,
        "kiwi_id": kid,
    }
    for code, zh, en, lat, lon, tz, kid in _RAW
}

# 建议拆票枢纽（长航线常见中转点），按大区粗分
DEFAULT_HUBS = [
    "IST", "DOH", "DXB", "AUH", "SIN", "HKG", "ICN", "NRT", "BKK", "KUL",
    "AMS", "FRA", "CDG", "HEL", "LHR", "MAD",
    "JFK", "LAX", "SFO", "ORD", "YYZ",
    "CAI", "ADD", "SYD",
]

# 用户很难在「同一城市不同机场」之间自己中转，这些枢纽只有一个主机场，安全
SINGLE_AIRPORT_HUBS = {
    "IST", "DOH", "DXB", "SIN", "HKG", "ICN", "BKK", "AMS", "FRA", "HEL",
    "MAD", "CAI", "ADD", "SYD", "JFK", "LAX", "SFO", "ORD", "YYZ", "NRT",
    "CDG", "LHR", "KUL",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    return text.upper() if text.isascii() else text


def register_city(
    code: str, zh: str, en: str, lat: float, lon: float, tz: float, kiwi_id: Optional[str]
) -> str:
    """把运行时从 Kiwi 解析到的城市登记进内存表（不改源码，进程内有效）。

    本地表只是「秒回缓存 + 离线兜底」，真正的城市解析以 Kiwi 的 places 接口为准。
    返回最终使用的城市码（重名时自动加序号）。
    """
    base = (code or "UNK").upper()[:4] or "UNK"
    code = base
    n = 1
    while code in CITIES and CITIES[code].get("kiwi_id") != kiwi_id:
        n += 1
        code = f"{base[:3]}{n}"
    CITIES[code] = {
        "code": code,
        "zh": zh or en or code,
        "en": en or zh or code,
        "lat": float(lat or 0.0),
        "lon": float(lon or 0.0),
        "tz": float(tz or 0.0),
        "kiwi_id": kiwi_id,
    }
    return code


def resolve_city(text: str) -> Optional[str]:
    """把任意输入（中文名 / 英文名 / 三字码）解析成城市码，失败返回 None。"""
    if not text:
        return None
    raw = unicodedata.normalize("NFKC", str(text)).strip()
    if raw in _ALIAS:
        return _ALIAS[raw]
    up = raw.upper()
    if up in _AIRPORT_TO_CITY:
        return _AIRPORT_TO_CITY[up]
    if up in CITIES:
        return up
    low = raw.lower()
    for code, c in CITIES.items():
        if low in (c["en"].lower(), c["zh"]):
            return code
    return None


def city(code: str) -> Optional[dict]:
    code = _AIRPORT_TO_CITY.get(code.upper(), code.upper())
    return CITIES.get(code)


def city_name(code: str) -> str:
    c = city(code)
    return c["zh"] if c else code


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_km(a: str, b: str) -> float:
    ca, cb = city(a), city(b)
    if not ca or not cb:
        return 0.0
    return haversine_km(ca["lat"], ca["lon"], cb["lat"], cb["lon"])


def tz_shift_h(a: str, b: str) -> float:
    """到达城市相对出发城市的时区差（小时，正=往东飞）。"""
    ca, cb = city(a), city(b)
    if not ca or not cb:
        return 0.0
    return cb["tz"] - ca["tz"]
