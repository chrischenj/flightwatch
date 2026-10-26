/* =========================================================================
 * 分舱段购票助手 · 前端（纯原生 JS，零外部依赖，离线可用）
 *
 * 数据契约：SPEC.md 第 6 节 api.py 输出的 JSON。
 * 页面结构：
 *   查询表单  ->  GET /api/plan?origin=..&destination=..&date=..&hubs=..
 *   结果区    ->  推荐结论 / 每个拆分方案的「成本 × 休息」三档对比 /
 *                 每段航班明细 / 升舱性价比提示 / 全部组合 / 风险提示
 * 兜底策略：接口失败、返回 ok=false 或 0 个方案时，回退加载 ./sample_plan.json，
 *           并在页面上明确标注「示例数据」。任何异常都渲染可见的错误块，不白屏。
 * ========================================================================= */
(function (root, factory) {
  'use strict';
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.FlightWatchUI = api;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { api.init(); });
    } else {
      api.init();
    }
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  /* ------------------------------------------------------------------ */
  /* 常量                                                                */
  /* ------------------------------------------------------------------ */

  var CONFIG = {
    apiPath: '/api/plan',
    samplePath: './sample_plan.json',
    timeoutMs: 12000,
    /* 实时查询走异步任务：每 900ms 拉一次进度，直到后端算完 */
    pollMs: 900
  };

  var CABIN_CN = {
    ECONOMY: '经济舱',
    PREMIUM_ECONOMY: '超级经济舱',
    BUSINESS: '公务舱',
    FIRST_CLASS: '头等舱'
  };

  var CABIN_ORDER = ['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST_CLASS'];

  /* 三种固定对比档位（其余混搭组合在「全部组合」里展示） */
  var PRESET_DEFS = [
    { key: 'economy', label: '全部经济舱', hint: '价格锚点' },
    { key: 'recommended', label: '建议组合', hint: '休息性价比最好' },
    { key: 'business', label: '全部公务舱', hint: '休息最大化' }
  ];

  /* ------------------------------------------------------------------ */
  /* 基础小工具                                                          */
  /* ------------------------------------------------------------------ */

  function isObj(v) { return !!v && typeof v === 'object'; }

  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }

  function arr(v) { return Array.isArray(v) ? v : []; }

  function str(v, fallback) {
    return typeof v === 'string' ? v : (v == null ? (fallback == null ? '' : fallback) : String(v));
  }

  function num(v, fallback) {
    var n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? n : (fallback == null ? 0 : fallback);
  }

  /* 与 num 的区别：null / undefined / 空串 / NaN 一律返回 fallback */
  function numOr(v, fallback) {
    if (v === null || v === undefined || v === '') return fallback;
    var n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? n : fallback;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function round(v, digits) {
    var p = Math.pow(10, digits == null ? 2 : digits);
    return Math.round(v * p) / p;
  }

  function uniqueStrings(list) {
    var seen = {};
    var out = [];
    arr(list).forEach(function (item) {
      if (item == null) return;
      var s = String(item).trim();
      if (!s || seen[s]) return;
      seen[s] = true;
      out.push(s);
    });
    return out;
  }

  function cabinCn(cabin) { return CABIN_CN[cabin] || str(cabin, '未知舱位'); }

  function currencyOfPlan(plan) {
    var legs = arr(plan && plan.legs);
    for (var i = 0; i < legs.length; i += 1) {
      var options = isObj(legs[i].options) ? legs[i].options : {};
      var keys = Object.keys(options);
      for (var k = 0; k < keys.length; k += 1) {
        var opt = options[keys[k]];
        if (isObj(opt) && opt.currency) return String(opt.currency);
      }
    }
    return 'CNY';
  }

  function queryCurrency(data) {
    return (data && data.query && data.query.currency) || 'CNY';
  }

  function sameCabins(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    for (var i = 0; i < a.length; i += 1) {
      if (String(a[i]) !== String(b[i])) return false;
    }
    return true;
  }

  function comboSignature(combo) {
    if (!combo || !Array.isArray(combo.cabins)) return '';
    return combo.cabins.join('|');
  }

  function planKey(plan) {
    return str(plan && plan.hub, 'HUB') + '#' + num(plan && plan.index, 0);
  }

  /* ---------------------------- DOM 助手 ---------------------------- */

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }

  function $all(sel, ctx) {
    return Array.prototype.slice.call((ctx || document).querySelectorAll(sel));
  }

  function appendChildren(node, children) {
    if (children == null || children === false) return;
    (Array.isArray(children) ? children : [children]).forEach(function (child) {
      if (child == null || child === false) return;
      node.appendChild(
        typeof child === 'string' || typeof child === 'number'
          ? document.createTextNode(String(child))
          : child
      );
    });
  }

  /* 轻量 createElement：attrs 支持 class / text / style / aria-* / dataset 等 */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value == null || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = String(value);
        else if (key === 'style') node.setAttribute('style', value);
        else if (key === 'dataset' && isObj(value)) {
          Object.keys(value).forEach(function (dk) { node.dataset[dk] = value[dk]; });
        } else if (key.indexOf('on') === 0 && typeof value === 'function') {
          node.addEventListener(key.slice(2), value);
        } else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, String(value));
      });
    }
    appendChildren(node, children);
    return node;
  }

  /* ------------------------------------------------------------------ */
  /* 格式化                                                              */
  /* ------------------------------------------------------------------ */

  function fmtMoney(value, currency) {
    var n = typeof value === 'number' ? value : parseFloat(value);
    if (!isFinite(n)) return '—';
    var rounded = Math.round(n);
    var sign = rounded < 0 ? '-' : '';
    var text = String(Math.abs(rounded)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    var prefix = (!currency || currency === 'CNY') ? '¥' : (String(currency) + ' ');
    return sign + prefix + text;
  }

  function fmtDeltaMoney(value, currency) {
    var n = num(value, NaN);
    if (!isFinite(n)) return '—';
    if (Math.abs(n) < 0.5) return '±¥0';
    return (n > 0 ? '+' : '-') + fmtMoney(Math.abs(n), currency);
  }

  function fmtHours(value) {
    var n = typeof value === 'number' ? value : parseFloat(value);
    if (!isFinite(n)) return '—';
    return (Math.round(n * 10) / 10).toFixed(1) + ' 小时';
  }

  function fmtDeltaHours(value) {
    var n = num(value, NaN);
    if (!isFinite(n)) return '—';
    if (Math.abs(n) < 0.05) return '±0.0 小时';
    return (n > 0 ? '+' : '-') + (Math.round(Math.abs(n) * 10) / 10).toFixed(1) + ' 小时';
  }

  function fmtDuration(value) {
    var minutes = num(value, NaN);
    if (!isFinite(minutes)) return '—';
    minutes = Math.round(minutes);
    var hours = Math.floor(minutes / 60);
    var mm = minutes % 60;
    if (!hours) return mm + ' 分钟';
    return hours + ' 小时' + (mm ? ' ' + mm + ' 分' : '');
  }

  /* '2026-12-10T00:20:00' / 带偏移 ISO -> '12-10 00:20' */
  function fmtLocalDateTime(value) {
    var s = str(value, '');
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s);
    if (!m) return s || '—';
    return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
  }

  function fmtPercent(value) {
    var n = num(value, NaN);
    if (!isFinite(n)) return '—';
    return Math.round(n * 100) + '%';
  }

  function fmtScore(value) {
    var n = num(value, NaN);
    if (!isFinite(n)) return '—';
    return Math.round(n) + '/100';
  }

  /* ------------------------------------------------------------------ */
  /* 查询参数                                                            */
  /* ------------------------------------------------------------------ */

  function parseHubs(raw) {
    var upper = String(raw == null ? '' : raw)
      .split(/[,，;；\s]+/)
      .map(function (item) { return item.trim().toUpperCase(); })
      .filter(Boolean);
    return uniqueStrings(upper);
  }

  function buildQuery(input) {
    var form = isObj(input) ? input : {};
    var origin = str(form.origin, '').trim();
    var destination = str(form.destination, '').trim();
    var date = str(form.date, '').trim();
    var retDate = str(form.retDate, '').trim();
    var hubs = parseHubs(form.hubs);
    var cabin = str(form.cabin, 'ECONOMY') || 'ECONOMY';
    var query = {
      origin: origin,
      destination: destination,
      date: date,
      retDate: retDate,
      hubs: hubs,
      cabin: cabin,
      offline: !!form.offline,
      refresh: form.refresh !== false
    };
    query.url = buildApiUrl(query);
    return query;
  }

  function buildApiUrl(input) {
    var parts = [];
    parts.push('origin=' + encodeURIComponent(str(input && input.origin, '').trim()));
    parts.push('destination=' + encodeURIComponent(str(input && input.destination, '').trim()));
    if (input && input.date) parts.push('date=' + encodeURIComponent(String(input.date).trim()));
    if (input && input.retDate) parts.push('return_date=' + encodeURIComponent(String(input.retDate).trim()));
    if (input && Array.isArray(input.hubs) && input.hubs.length) {
      parts.push('hubs=' + input.hubs.map(function (hub) { return encodeURIComponent(hub); }).join(','));
    }
    if (input && input.cabin && input.cabin !== 'ECONOMY') {
      parts.push('cabin=' + encodeURIComponent(input.cabin));
    }
    /* 离线演示优先让后端走 fixture 夹具（server.py 支持 offline=1）；
       后端不在时 runQuery 会回退到本地 sample_plan.json */
    if (input && input.offline) parts.push('offline=1');
    /* 默认每次查询都跳过本地价格缓存，直接取 Kiwi 的最新价 */
    if (input && input.refresh) parts.push('refresh=1');
    return CONFIG.apiPath + '?' + parts.join('&');
  }

  /* ------------------------------------------------------------------ */
  /* 数据归一化（对后端返回做容错，任何缺字段都降级为占位而不是抛错）    */
  /* ------------------------------------------------------------------ */

  function normalizeData(raw) {
    if (!isObj(raw)) return null;
    var data = raw;
    data.query = isObj(data.query) ? data.query : {};
    data.baseline = isObj(data.baseline) ? data.baseline : {};
    data.source = isObj(data.source) ? data.source : { name: '', live: null };
    data.errors = uniqueStrings(arr(data.errors));
    data.warnings = uniqueStrings(arr(data.warnings));
    data.price_note = str(data.price_note, '');
    data.mode = str(data.mode, 'oneway');
    data.strategies = arr(data.strategies).filter(isObj);
    data.plans = arr(data.plans).filter(isObj).map(normalizePlan);
    data.plans = data.plans.filter(function (plan) {
      return plan.legs.length > 0 && plan.combos.length > 0;
    });
    /* 往返模式：回程方向的拆分方案 */
    data.ret_plans = arr(data.ret_plans).filter(isObj).map(normalizePlan);
    data.ret_plans = data.ret_plans.filter(function (plan) {
      return plan.legs.length > 0 && plan.combos.length > 0;
    });

    data.recommendation = isObj(data.recommendation) ? data.recommendation : {};
    data.recommendation.headline = str(data.recommendation.headline, '');
    data.recommendation.reasons = uniqueStrings(arr(data.recommendation.reasons));
    data.recommendation.warnings = uniqueStrings(arr(data.recommendation.warnings));
    data.recommendation.combo = isObj(data.recommendation.combo)
      ? data.recommendation.combo
      : null;

    if (data.recommendation.combo && Array.isArray(data.recommendation.combo.cabins)) {
      var recHub = data.recommendation.combo.plan_hub;
      var owner = null;
      data.plans.forEach(function (plan) {
        if (owner) return;
        if (recHub && plan.hub && recHub !== plan.hub) return;
        if (findComboByCabins(plan, data.recommendation.combo.cabins)) owner = plan;
      });
      if (owner) {
        data.recommendation.combo = finalizeCombo(
          coerceCombo(data.recommendation.combo, owner),
          owner,
          pickBaseCombo(owner)
        );
      }
    }
    return data;
  }

  function normalizePlan(plan, index) {
    plan.index = index;
    plan.hub = str(plan.hub, '');
    plan.hub_name = str(plan.hub_name, plan.hub || '未知枢纽');
    plan.label = str(plan.label, plan.hub ? ('拆分方案：' + plan.hub_name) : ('方案 ' + (index + 1)));
    plan.note = str(plan.note, '');
    plan.layovers_min = arr(plan.layovers_min).map(function (v) { return num(v, 0); });

    plan.legs = arr(plan.legs).filter(isObj).map(normalizeLeg);

    var combos = arr(plan.combos).filter(isObj).map(function (combo) {
      return coerceCombo(combo, plan);
    });
    if (!combos.length) combos = synthCombos(plan);

    var base = pickBaseCombo(plan, combos);
    plan.combos = combos.map(function (combo) { return finalizeCombo(combo, plan, base); });

    var pareto = arr(plan.pareto)
      .map(function (v) { return num(v, -1); })
      .filter(function (i) { return i >= 0 && i < plan.combos.length; });
    plan.pareto = pareto.length ? pareto : computePareto(plan.combos);

    plan.best_value = isObj(plan.best_value) ? normalizeBestValue(plan.best_value) : null;
    return plan;
  }

  function normalizeLeg(leg, index) {
    leg.index = isNum(leg.index) ? Number(leg.index) : index;
    leg.origin = str(leg.origin, '');
    leg.destination = str(leg.destination, '');
    leg.distance_km = num(leg.distance_km, 0);
    leg.tz_shift_h = num(leg.tz_shift_h, 0);

    var rawOptions = isObj(leg.options) ? leg.options : {};
    var options = {};
    CABIN_ORDER.forEach(function (cabin) {
      if (isObj(rawOptions[cabin])) options[cabin] = rawOptions[cabin];
    });
    Object.keys(rawOptions).forEach(function (cabin) {
      if (isObj(rawOptions[cabin]) && !options[cabin]) options[cabin] = rawOptions[cabin];
    });
    leg.options = options;
    leg.rest = normalizeRest(leg.rest);
    return leg;
  }

  function normalizeRest(rest) {
    if (!isObj(rest)) {
      return {
        missing: true,
        score: null,
        effective_rest_h: {},
        sleep_h: null,
        night_ratio: null,
        verdict: ''
      };
    }
    var effective = {};
    if (isObj(rest.effective_rest_h)) {
      Object.keys(rest.effective_rest_h).forEach(function (cabin) {
        var value = rest.effective_rest_h[cabin];
        if (value != null && isFinite(parseFloat(value))) effective[cabin] = parseFloat(value);
      });
    }
    return {
      missing: false,
      score: numOr(rest.score, null),
      effective_rest_h: effective,
      sleep_h: numOr(rest.sleep_h, null),
      night_ratio: numOr(rest.night_ratio, null),
      verdict: str(rest.verdict, ''),
      cost_per_rest_h: numOr(rest.cost_per_rest_h, null),
      different_flight: !!rest.different_flight,
      upgrade_note: str(rest.upgrade_note, '')
    };
  }

  function normalizeBestValue(best) {
    var legIndex = num(best.leg_index, -1);
    if (legIndex < 0) return null;
    return {
      leg_index: legIndex,
      cabin: str(best.cabin, 'BUSINESS'),
      extra_cost: numOr(best.extra_cost, null),
      extra_rest_h: numOr(best.extra_rest_h, null),
      cost_per_rest_h: numOr(best.cost_per_rest_h, null)
    };
  }

  /* 把后端给的 combo 转成前端对象；缺的数值字段稍后从 legs 推算 */
  function coerceCombo(raw, plan) {
    var cabins = arr(raw.cabins).map(function (cabin) { return String(cabin); });
    if (!cabins.length && Array.isArray(raw.cabins_cn)) {
      /* 只有中文名时尽量反推，失败则补经济舱 */
      raw.cabins_cn.forEach(function (cn) {
        var found = null;
        Object.keys(CABIN_CN).forEach(function (key) { if (CABIN_CN[key] === cn) found = key; });
        cabins.push(found || 'ECONOMY');
      });
    }
    var legCount = plan.legs.length;
    while (cabins.length < legCount) cabins.push('ECONOMY');
    if (legCount && cabins.length > legCount) cabins = cabins.slice(0, legCount);

    var combo = {
      plan_hub: str(raw.plan_hub, plan.hub),
      cabins: cabins,
      cabins_cn: arr(raw.cabins_cn).length === cabins.length
        ? arr(raw.cabins_cn).map(String)
        : cabins.map(cabinCn),
      total_price: numOr(raw.total_price, null),
      currency: str(raw.currency, currencyOfPlan(plan)),
      total_duration_min: numOr(raw.total_duration_min, null),
      total_ground_min: numOr(raw.total_ground_min, null),
      comfort_score: numOr(raw.comfort_score, null),
      price_delta: numOr(raw.price_delta, null),
      rest_gain: numOr(raw.rest_gain, null),
      value_per_rest_hour: numOr(raw.value_per_rest_hour, null),
      legs: arr(raw.legs)
    };
    if (combo.total_price == null) combo.total_price = comboPrice(plan, cabins);
    if (combo.total_duration_min == null) combo.total_duration_min = comboDuration(plan);
    if (combo.total_ground_min == null) combo.total_ground_min = sumLayovers(plan);
    if (combo.comfort_score == null) combo.comfort_score = comboRest(plan, cabins);
    return combo;
  }

  /* 补齐相对基准的差价 / 休息增量 / 每多睡 1 小时成本（始终由前端重算，保证自洽） */
  function finalizeCombo(combo, plan, base) {
    var currency = combo.currency || currencyOfPlan(plan);
    var ecoCabins = plan.legs.map(function () { return 'ECONOMY'; });
    var basePrice = base && isNum(base.total_price) ? base.total_price : comboPrice(plan, ecoCabins);
    var baseRest = base && isNum(base.comfort_score) ? base.comfort_score : comboRest(plan, ecoCabins);

    combo.currency = currency;
    combo.total_price = num(combo.total_price, 0);
    combo.comfort_score = num(combo.comfort_score, 0);
    combo.total_duration_min = num(combo.total_duration_min, comboDuration(plan));
    combo.total_ground_min = num(combo.total_ground_min, sumLayovers(plan));
    combo.price_delta = round(combo.total_price - basePrice, 2);
    combo.rest_gain = round(combo.comfort_score - baseRest, 2);
    combo.value_per_rest_hour = combo.rest_gain > 0.05
      ? round(combo.price_delta / combo.rest_gain, 2)
      : null;
    if (!combo.cabins_cn || combo.cabins_cn.length !== combo.cabins.length) {
      combo.cabins_cn = combo.cabins.map(cabinCn);
    }
    return combo;
  }

  function optionFor(leg, cabin) {
    if (!leg || !isObj(leg.options)) return null;
    return isObj(leg.options[cabin]) ? leg.options[cabin] : null;
  }

  function legPrice(leg, cabin) {
    var option = optionFor(leg, cabin);
    return option ? numOr(option.price, null) : null;
  }

  function legDuration(leg) {
    var order = CABIN_ORDER.concat(['ECONOMY']);
    for (var i = 0; i < order.length; i += 1) {
      var option = optionFor(leg, order[i]);
      if (option && isNum(option.duration_min)) return option.duration_min;
    }
    var keys = Object.keys((leg && leg.options) || {});
    for (var k = 0; k < keys.length; k += 1) {
      if (isNum(leg.options[keys[k]].duration_min)) return leg.options[keys[k]].duration_min;
    }
    return 0;
  }

  function comboPrice(plan, cabins) {
    var total = 0;
    plan.legs.forEach(function (leg, i) {
      var price = legPrice(leg, cabins[i]);
      if (price != null) total += price;
    });
    return round(total, 2);
  }

  function comboRest(plan, cabins) {
    var total = 0;
    plan.legs.forEach(function (leg, i) {
      var rest = leg.rest && !leg.rest.missing ? leg.rest.effective_rest_h : null;
      if (rest && isNum(rest[cabins[i]])) total += rest[cabins[i]];
    });
    return round(total, 2);
  }

  function comboDuration(plan) {
    var total = sumLayovers(plan);
    plan.legs.forEach(function (leg) { total += legDuration(leg); });
    return Math.round(total);
  }

  function sumLayovers(plan) {
    return arr(plan.layovers_min).reduce(function (sum, value) { return sum + num(value, 0); }, 0);
  }

  /* 超集：给定方案缺 combos 时自己枚举（正常 api.py 会给出，这里只做容错） */
  function synthCombos(plan) {
    if (!plan || !plan.legs.length) return [];
    var cabinLists = plan.legs.map(function (leg) {
      var keys = Object.keys(leg.options || {}).filter(function (cabin) {
        return isObj(leg.options[cabin]);
      });
      keys.sort(function (a, b) {
        return CABIN_ORDER.indexOf(a) - CABIN_ORDER.indexOf(b);
      });
      return keys.length ? keys : ['ECONOMY'];
    });

    var combos = [];
    function walk(index, chosen) {
      if (index === cabinLists.length) {
        var cabins = chosen.slice();
        combos.push({
          plan_hub: plan.hub,
          cabins: cabins,
          cabins_cn: cabins.map(cabinCn),
          total_price: comboPrice(plan, cabins),
          currency: currencyOfPlan(plan),
          total_duration_min: comboDuration(plan),
          total_ground_min: sumLayovers(plan),
          comfort_score: comboRest(plan, cabins),
          price_delta: null,
          rest_gain: null,
          value_per_rest_hour: null,
          legs: []
        });
        return;
      }
      cabinLists[index].forEach(function (cabin) {
        chosen.push(cabin);
        walk(index + 1, chosen);
        chosen.pop();
      });
    }
    walk(0, []);
    return combos;
  }

  function pickBaseCombo(plan, passedCombos) {
    var combos = arr(passedCombos || plan.combos);
    var ecoCabins = plan.legs.map(function () { return 'ECONOMY'; });
    var eco = findComboByCabins(plan, ecoCabins, combos);
    if (eco) return eco;
    if (!combos.length) return null;
    return combos.slice().sort(function (a, b) {
      return num(a.total_price, Infinity) - num(b.total_price, Infinity);
    })[0];
  }

  function findComboByCabins(plan, cabins, passedCombos) {
    var combos = arr(passedCombos || plan.combos);
    for (var i = 0; i < combos.length; i += 1) {
      if (sameCabins(combos[i].cabins, cabins)) return combos[i];
    }
    return null;
  }

  function computePareto(combos) {
    var list = arr(combos);
    var indexes = [];
    list.forEach(function (combo, i) {
      var dominated = list.some(function (other, j) {
        if (i === j) return false;
        var price = num(other.total_price, 0);
        var rest = num(other.rest_gain, 0);
        var thisPrice = num(combo.total_price, 0);
        var thisRest = num(combo.rest_gain, 0);
        return price <= thisPrice && rest >= thisRest && (price < thisPrice || rest > thisRest);
      });
      if (!dominated) indexes.push(i);
    });
    return indexes;
  }

  /* ---------------------------- 推荐组合 ---------------------------- */

  function findRecommendedCombo(plan, data) {
    if (!plan || !plan.combos || !plan.combos.length) return null;
    var recCombo = data && data.recommendation && data.recommendation.combo;
    if (isObj(recCombo) && Array.isArray(recCombo.cabins)) {
      var hubOk = !recCombo.plan_hub || !plan.hub || recCombo.plan_hub === plan.hub;
      if (hubOk) {
        var hit = findComboByCabins(plan, recCombo.cabins);
        if (hit) return hit;
      }
    }
    if (isObj(plan.best_value)) {
      var legIndex = num(plan.best_value.leg_index, -1);
      if (legIndex >= 0 && legIndex < plan.legs.length) {
        var cabins = plan.legs.map(function () { return 'ECONOMY'; });
        cabins[legIndex] = str(plan.best_value.cabin, 'BUSINESS');
        var byBest = findComboByCabins(plan, cabins);
        if (byBest) return byBest;
      }
    }
    var best = null;
    plan.combos.forEach(function (combo) {
      if (num(combo.rest_gain, 0) <= 0.05) return;
      if (combo.value_per_rest_hour == null || !isFinite(combo.value_per_rest_hour)) return;
      if (!best || combo.value_per_rest_hour < best.value_per_rest_hour) best = combo;
    });
    return best;
  }

  function getPresets(plan, data) {
    var allEcoCabins = plan.legs.map(function () { return 'ECONOMY'; });
    var allBizCabins = plan.legs.map(function () { return 'BUSINESS'; });
    var recommended = findRecommendedCombo(plan, data);
    var map = {
      economy: findComboByCabins(plan, allEcoCabins),
      recommended: recommended,
      business: findComboByCabins(plan, allBizCabins)
    };
    return PRESET_DEFS.map(function (def) {
      return {
        key: def.key,
        label: def.label,
        hint: def.hint,
        combo: map[def.key] || null
      };
    });
  }

  function defaultPresetKey(plan, data) {
    return findRecommendedCombo(plan, data) ? 'recommended' : 'economy';
  }

  function pickRow(rows, wanted) {
    var list = arr(rows);
    if (!list.length) return null;
    var direct = null;
    list.forEach(function (row) { if (!direct && row.key === wanted) direct = row; });
    if (direct) return direct;
    var byPreset = null;
    list.forEach(function (row) {
      if (byPreset) return;
      if (row.presets.some(function (preset) { return preset.key === wanted; })) byPreset = row;
    });
    return byPreset || list[0];
  }

  /* 三种档位可能重合（例如推荐组合就是全经济舱）：重合的行合并展示，避免重复条 */
  function buildPresetRows(plan, data) {
    var presets = getPresets(plan, data);
    var rows = [];
    presets.forEach(function (preset) {
      if (!preset.combo) return;
      var signature = comboSignature(preset.combo);
      if (!signature) return;
      var existing = null;
      rows.forEach(function (row) { if (!existing && row.sig === signature) existing = row; });
      if (existing) {
        existing.presets.push(preset);
        return;
      }
      rows.push({
        key: preset.key,
        sig: signature,
        presets: [preset],
        combo: preset.combo,
        isRecommended: false,
        isPareto: false
      });
    });
    rows.forEach(function (row) {
      row.isRecommended = row.presets.some(function (preset) { return preset.key === 'recommended'; });
      var comboIndex = plan.combos.indexOf(row.combo);
      row.isPareto = arr(plan.pareto).indexOf(comboIndex) >= 0;
    });
    return rows;
  }

  /* ------------------------------------------------------------------ */
  /* 渲染：总入口与数据源状态                                            */
  /* ------------------------------------------------------------------ */

  var dom = {};
  var state = {
    data: null,
    meta: null,
    selection: {},
    running: false,
    /* 每次查询递增；旧查询发现 runId 变了就自动让位，
       这样任何一次卡住的查询都不会让页面「再也搜不动」。 */
    runId: 0
  };
  var MAX_JOB_SECONDS = 360;   /* 单次查询最多等 6 分钟，超时就中止并允许重试 */

  function init() {
    if (typeof document === 'undefined') return;
    dom.form = $('#query-form');
    dom.results = $('#results');
    dom.status = $('#status');
    dom.appError = $('#app-error');
    dom.sourceBadge = $('#source-badge');
    dom.origin = $('#origin');
    dom.destination = $('#destination');
    dom.date = $('#date');
    dom.retDate = $('#ret-date');
    dom.cabin = $('#cabin');
    dom.hubs = $('#hubs');
    dom.offline = $('#offline');
    dom.refresh = $('#refresh');
    if (!dom.form || !dom.results) return;

    dom.form.addEventListener('submit', function (event) {
      event.preventDefault();
      runQuery();
    });
    dom.results.addEventListener('click', onResultsClick);
    if (dom.appError) dom.appError.addEventListener('click', onResultsClick);
    if (typeof window !== 'undefined') {
      window.addEventListener('error', function (event) {
        showRuntimeError(str(event && event.message, '未知脚本错误'));
      });
      window.addEventListener('unhandledrejection', function (event) {
        showRuntimeError('异步任务异常：' + errorDetail(event && event.reason));
      });
    }
    if (!maybeRunSelfTest()) runQuery();
  }

  /* ------------------------------------------------------------------ */
  /* 自检：地址栏加 #selftest=2 会连续跑 N 次搜索（默认 2 次），           */
  /* 把每轮的 state / 方案数 / 是否回退示例数据打到页面上，便于定位问题。 */
  /* ------------------------------------------------------------------ */
  function maybeRunSelfTest() {
    if (typeof location === 'undefined' || !location.hash) return false;
    var m = /selftest(?:=(\d+))?/.exec(location.hash);
    if (!m) return false;

    var rounds = Math.min(4, Math.max(1, parseInt(m[1] || '2', 10)));
    var routes = [
      { origin: 'BJS', destination: 'JFK', date: '2026-12-10', retDate: '2026-12-18', hubs: 'IST,DOH' },
      { origin: 'CAN', destination: 'CDG', date: '2026-12-18', hubs: 'IST,DOH' },
      { origin: 'CTU', destination: 'SYD', date: '2026-12-20', hubs: 'SIN,HKG' },
      { origin: 'SHA', destination: 'LHR', date: '2026-12-15', hubs: 'IST,DOH' }
    ];
    var log = [];
    var box = el('pre', {
      id: 'selftest-log',
      style: 'margin:16px;padding:12px;background:#111;color:#0f0;font-size:12px;white-space:pre-wrap;border-radius:8px'
    });
    if (document.body) document.body.appendChild(box);

    function flush() { box.textContent = 'SELFTEST\\n' + log.join('\\n'); }

    function waitIdle(cb, deadline) {
      if (!state.running) { cb(); return; }
      if (Date.now() > deadline) { log.push('    !! 卡住：state.running 一直为 true'); flush(); cb(); return; }
      setTimeout(function () { waitIdle(cb, deadline); }, 500);
    }

    function step(i) {
      if (i >= rounds) {
        log.push('SELFTEST-DONE rounds=' + i);
        flush();
        document.title = 'SELFTEST-DONE';
        return;
      }
      var r = routes[i % routes.length];
      if (dom.origin) dom.origin.value = r.origin;
      if (dom.destination) dom.destination.value = r.destination;
      if (dom.date) dom.date.value = r.date;
      if (dom.retDate) dom.retDate.value = r.retDate || '';
      if (dom.hubs) dom.hubs.value = r.hubs;
      if (dom.offline) dom.offline.checked = false;
      log.push('--- round ' + (i + 1) + ' ' + r.origin + '->' + r.destination + ' ' + r.date);
      flush();
      var t0 = Date.now();
      runQuery();
      waitIdle(function () {
        var plans = (state.data && state.data.plans) || [];
        log.push('    ' + Math.round((Date.now() - t0) / 1000) + 's running=' + state.running +
          ' plans=' + plans.length + ' sample=' + !!((state.meta || {}).sample));
        flush();
        setTimeout(function () { step(i + 1); }, 400);
      }, Date.now() + 240000);
    }

    step(0);
    return true;
  }

  function readForm() {
    return {
      origin: dom.origin ? dom.origin.value : '',
      destination: dom.destination ? dom.destination.value : '',
      date: dom.date ? dom.date.value : '',
      retDate: dom.retDate ? dom.retDate.value : '',
      cabin: dom.cabin ? dom.cabin.value : 'ECONOMY',
      hubs: dom.hubs ? dom.hubs.value : '',
      offline: !!(dom.offline && dom.offline.checked),
      refresh: dom.refresh ? dom.refresh.checked : true
    };
  }

  function setStatus(message, tone) {
    if (!dom.status) return;
    dom.status.textContent = message;
    dom.status.className = 'status' + (tone ? ' ' + tone : '');
  }

  function renderLoading(message) {
    if (!dom.results) return;
    dom.results.replaceChildren(el('div', { class: 'notice loading', role: 'status' }, [
      el('span', { class: 'spinner', 'aria-hidden': 'true' }),
      el('span', {}, message || '正在加载…')
    ]));
  }

  function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* 实时查询要走 /api/plan/start + /api/job/<id> 轮询：一次查询要几十秒，
     浏览器不能干等一个 HTTP 响应（以前 12 秒超时就误判失败、回退成示例数据）。 */
  function startJob(query) {
    var startUrl = String(query.url).replace('/api/plan', '/api/plan/start');
    return fetchJson(startUrl, 15000);
  }

  function pollJob(jobId, startedAt, token) {
    return fetchJson('/api/job/' + encodeURIComponent(jobId), 20000).then(function (st) {
      if (token !== state.runId) throw CANCELLED;
      if (!isObj(st)) throw new Error('任务状态返回异常');
      var elapsed = Math.round((Date.now() - startedAt) / 1000);
      if (elapsed > MAX_JOB_SECONDS) {
        throw new Error('查询超过 ' + MAX_JOB_SECONDS + ' 秒仍未完成，已中止（可以直接再点一次查询）');
      }
      if (st.state === 'running') {
        var msg = '实时查询中 · ' + str(st.label, '拉取报价') +
          (st.total ? '（' + num(st.done, 0) + '/' + num(st.total, 0) + '）' : '') +
          ' · 已用 ' + elapsed + ' 秒';
        setStatus(msg, 'busy');
        renderLoading(msg);
        return delay(CONFIG.pollMs).then(function () { return pollJob(jobId, startedAt, token); });
      }
      if (st.state === 'done') return st.payload;
      throw new Error(str(st.error, '查询任务失败'));
    });
  }

  var CANCELLED = { cancelled: true };

  function runQuery() {
    var input = readForm();
    if (!input.origin.trim() || !input.destination.trim()) {
      setStatus('请先填写出发城市和到达城市（可以填中文名或三字码，如 BJS / JFK）。', 'error');
      return;
    }
    if (!input.date) {
      setStatus('请选择出发日期。', 'error');
      return;
    }

    var query = buildQuery(input);
    var startedAt = Date.now();
    var token = ++state.runId;          /* 让上一次（可能卡住的）查询自动作废 */
    if (state.running) {
      setStatus('已放弃上一次查询，正在用新条件重新查询…', 'warn');
    }
    state.running = true;
    setStatus(input.offline ? '离线演示：正在生成方案…' : '正在实时查询报价（首次约 30–90 秒）…', 'busy');
    renderLoading(input.offline
      ? '正在生成离线方案…'
      : '正在实时查询报价，首次约 30–90 秒，进度会自动更新…');
    startJob(query)
      .then(function (res) {
        if (token !== state.runId) throw CANCELLED;
        if (isObj(res) && Array.isArray(res.plans)) return res;   // 兼容同步返回
        if (isObj(res) && res.job) return pollJob(res.job, startedAt, token);
        if (isObj(res) && res.ok === false) {
          throw new Error(uniqueStrings(arr(res.errors)).join('；') || '接口返回 ok=false');
        }
        throw new Error('启动查询任务失败');
      })
      .then(function (raw) {
        if (token !== state.runId) return;
        if (!isObj(raw)) throw new Error('接口返回内容不是对象');
        var data = normalizeData(raw);
        var seconds = Math.round((Date.now() - startedAt) / 1000);
        if (raw.ok === false && (!data || !data.plans.length)) {
          // 后端明确说「没拼出方案」：按空结果展示原因，绝不拿示例数据顶替，
          // 否则用户会以为「搜了没反应 / 结果还是上一次的」。
          var why = (data && data.errors && data.errors[0]) || '可能这两城之间航班太少';
          setStatus('查询完成，但没有可行的分段方案（' + why + '）· 用时 ' + seconds + ' 秒', 'warn');
          render(data, { sample: false, reason: '' });
          return;
        }
        if (!data || !data.plans.length) throw new Error('接口没有返回任何可行的拆分方案');
        setStatus('已加载' + (input.offline ? '后端离线数据（fixture）' : '实时数据') +
          (data.source && data.source.name ? ' · ' + data.source.name : '') +
          ' · 用时 ' + seconds + ' 秒' +
          (data.generated_at ? ' · 数据抓取于 ' + String(data.generated_at).slice(11, 19) : ''), 'ok');
        render(data, { sample: false, reason: '' });
      })
      .catch(function (error) {
        if (error === CANCELLED || (error && error.cancelled) || token !== state.runId) return;
        var detail = errorDetail(error);
        var hint = '';
        if (/plan\/start|404|not found|后端版本/i.test(detail)) {
          hint = '后端像是旧版本（没有异步查询接口）：请关掉那个黑色窗口，重新双击「启动网页版.bat」。';
        } else if (/超时|timeout|abort|中止/i.test(detail)) {
          hint = '网络或接口太慢：稍等几秒点「重新查询」再试一次；也可以先勾「离线演示」看界面。';
        }
        setStatus('实时查询失败：' + detail, 'error');
        renderFatal('实时查询失败：' + detail, hint);
      })
      .then(function () {
        if (token === state.runId) state.running = false;
      }, function () {
        if (token === state.runId) state.running = false;
      });
  }

  function loadSample(reason) {
    renderLoading('正在加载本地示例数据…');
    return fetchJson(CONFIG.samplePath, CONFIG.timeoutMs)
      .then(function (raw) {
        var data = normalizeData(raw);
        if (!data || !data.plans.length) throw new Error('示例数据为空或缺少 plans');
        setStatus('已加载示例数据（' + reason + '）', 'warn');
        render(data, { sample: true, reason: reason });
      })
      .catch(function (error) {
        var detail = '示例数据加载失败：' + errorDetail(error);
        setStatus('示例数据加载失败：请确认 flightwatch/web/sample_plan.json 存在且为合法 JSON。', 'error');
        renderFatal(detail,
          '如果直接双击 index.html（file:// 协议）打开，浏览器会阻止读取本地 JSON。' +
          '请用静态服务器打开：在项目根目录执行 python -m http.server 8899 -d flightwatch/web，然后访问 http://127.0.0.1:8899/。');
      });
  }

  function fetchJson(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      if (typeof fetch !== 'function') {
        reject(new Error('当前浏览器不支持 fetch'));
        return;
      }
      var controller = null;
      var timer = null;
      try {
        if (typeof AbortController === 'function') controller = new AbortController();
      } catch (error) {
        controller = null;
      }
      var options = {
        method: 'GET',
        cache: 'no-store',
        headers: { Accept: 'application/json' }
      };
      if (controller) options.signal = controller.signal;
      if (controller && timeoutMs > 0) {
        timer = setTimeout(function () {
          try { controller.abort(); } catch (error) { /* ignore */ }
        }, timeoutMs);
      }
      fetch(url, options).then(function (response) {
        if (timer) clearTimeout(timer);
        if (!response.ok) {
          /* 关键：4xx/5xx 时后端会把真正的原因放在 errors[0]，必须读出来，
             否则用户只看到 "HTTP 400" 这种没用的信息。 */
          response.json().then(function (body) {
            var list = (body && Array.isArray(body.errors)) ? body.errors : [];
            var msg = list.length ? String(list[0]) : ('HTTP ' + response.status);
            reject(new Error(msg + '（HTTP ' + response.status + '）'));
          }, function () {
            reject(new Error('HTTP ' + response.status));
          });
          return;
        }
        response.json().then(resolve, function () {
          reject(new Error('返回内容不是合法 JSON'));
        });
      }, function (error) {
        if (timer) clearTimeout(timer);
        reject(error);
      });
    });
  }

  function errorDetail(error) {
    if (!error) return '未知错误';
    if (typeof error === 'string') return error;
    if (error.name === 'AbortError') {
      return '请求超时（超过 ' + Math.round(CONFIG.timeoutMs / 1000) + ' 秒）';
    }
    if (error.message) return String(error.message);
    return String(error);
  }

  function render(data, meta) {
    if (!dom.results) return;
    state.data = data;
    state.meta = meta || { sample: false, reason: '' };
    state.selection = {};
    updateSourceBadge(data, state.meta);

    var fragment = document.createDocumentFragment();
    fragment.appendChild(renderBanner(data, state.meta));

    if (data.price_note) {
      fragment.appendChild(el('p', { class: 'price-note' }, data.price_note));
    }

    if (data.errors.length) {
      fragment.appendChild(el('div', { class: 'notice warn small', role: 'status' }, [
        el('strong', {}, '部分查询有错误：'),
        el('span', {}, data.errors.join('；'))
      ]));
    }

    var recommendation = renderRecommendation(data);
    if (recommendation) fragment.appendChild(recommendation);

    if (data.mode === 'roundtrip') {
      var strategyTable = renderStrategies(data);
      if (strategyTable) fragment.appendChild(strategyTable);
      if (data.plans.length) {
        fragment.appendChild(renderPlans(data, {
          title: '去程拆分方案', titleId: 'out-plans-title',
          subtitle: '共 ' + data.plans.length + ' 个候选中转枢纽 · 点击三档可切换高亮'
        }));
      }
      if (data.ret_plans.length) {
        fragment.appendChild(renderPlans(
          Object.assign({}, data, { plans: data.ret_plans }),
          { title: '回程拆分方案', titleId: 'ret-plans-title',
            subtitle: '共 ' + data.ret_plans.length + ' 个候选中转枢纽 · 点击三档可切换高亮' }
        ));
      }
      if (!data.plans.length && !data.ret_plans.length) {
        fragment.appendChild(renderEmpty(data, state.meta));
      }
    } else if (!data.plans.length) {
      fragment.appendChild(renderEmpty(data, state.meta));
    } else {
      fragment.appendChild(renderPlans(data));
    }

    var warnings = renderWarnings(data);
    if (warnings) fragment.appendChild(warnings);

    fragment.appendChild(renderMeta(data, state.meta));
    dom.results.replaceChildren(fragment);
    applyAllSelections(data);
  }

  function updateSourceBadge(data, meta) {
    if (!dom.sourceBadge) return;
    var sample = !!(meta && meta.sample);
    var live = !!(data && data.source && data.source.live);
    dom.sourceBadge.textContent = sample ? '示例数据 · 非实时' : (live ? '实时数据' : '离线数据');
    dom.sourceBadge.className = 'source-badge ' + (sample ? 'is-sample' : 'is-live');
    dom.sourceBadge.title = sample
      ? '当前展示本地示例数据，不是真实报价'
      : ('数据来源：' + ((data && data.source && data.source.name) || '后端接口'));
  }

  function renderBanner(data, meta) {
    var sample = !!(meta && meta.sample);
    var live = !!(data.source && data.source.live);
    var banner = el('section', {
      class: 'data-banner ' + (sample ? 'is-sample' : 'is-live'),
      role: 'status'
    });
    banner.appendChild(el('span', { class: 'banner-pill' },
      sample ? '示例数据' : (live ? '实时数据' : '离线数据')
    ));

    var body = el('div', { class: 'banner-body' });
    if (sample) {
      body.appendChild(el('strong', {}, '以下为本地示例数据，不是真实报价。'));
      body.appendChild(el('span', {}, (meta.reason ? '原因：' + meta.reason + '。' : '') +
        '数据来自 flightwatch/web/sample_plan.json，仅用于演示界面与结论；连上后端后会自动显示实时结果。'));
    } else {
      body.appendChild(el('strong', {},
        '数据来源：' + ((data.source && data.source.name) || '后端接口')));
      if (data.source && data.source.live === false) {
        body.appendChild(el('span', {}, '（后端返回的是离线 / 夹具数据）'));
        body.appendChild(el('span', {}, '离线夹具数据只用于演示算法与界面，不是真实报价。'));
      } else {
        body.appendChild(el('span', {}, '价格为公开接口实时枚举，可能存在缓存延迟，下单前必须复核。'));
      }
    }
    banner.appendChild(body);

    if (sample) {
      banner.appendChild(el('button', {
        type: 'button', class: 'btn ghost small', 'data-action': 'retry-live'
      }, '改用实时查询'));
    }
    return banner;
  }

  function renderRecommendation(data) {
    var rec = data.recommendation || {};
    var hasCombo = isObj(rec.combo);
    if (!rec.headline && !rec.reasons.length && !hasCombo) return null;

    var section = el('section', { class: 'panel recommend', 'aria-labelledby': 'rec-title' }, [
      el('div', { class: 'rec-top' }, [
        el('span', { class: 'pill rec-pill' }, '推荐'),
        el('span', { class: 'rec-hint' }, '按「多花的钱 / 多换回的休息」综合排序')
      ]),
      el('h2', { id: 'rec-title', class: 'rec-headline' },
        rec.headline || '当前条件下未生成一句话结论，请参考下方方案对比。')
    ]);

    if (hasCombo) {
      var combo = rec.combo;
      var ownerPlan = null;
      data.plans.forEach(function (plan) {
        if (!ownerPlan && (!combo.plan_hub || plan.hub === combo.plan_hub)) ownerPlan = plan;
      });
      var strip = el('div', { class: 'rec-combo' }, [
        el('span', { class: 'rec-combo-label' }, '建议组合'),
        el('b', {}, arr(combo.cabins_cn).join(' + ') || arr(combo.cabins).map(cabinCn).join(' + ')),
        el('span', { class: 'rec-combo-item' }, fmtMoney(combo.total_price, combo.currency)),
        el('span', { class: 'rec-combo-item' }, '有效休息 ' + fmtHours(combo.comfort_score)),
        el('span', { class: 'rec-combo-item' }, '比全经济舱 ' +
          fmtDeltaMoney(combo.price_delta, combo.currency) + ' / ' + fmtDeltaHours(combo.rest_gain) + ' 休息'),
        ownerPlan ? el('span', { class: 'rec-combo-plan' }, '方案：' + ownerPlan.hub_name + '（' + ownerPlan.label + '）') : null
      ]);
      section.appendChild(strip);
    }

    if (rec.reasons.length) {
      section.appendChild(el('ol', { class: 'rec-reasons' }, rec.reasons.map(function (reason) {
        return el('li', {}, reason);
      })));
    } else {
      section.appendChild(el('p', { class: 'rec-empty' }, '接口未给出推荐理由（reasons 为空）。'));
    }
    return section;
  }

  /* 往返模式：一张往返票 / 两张单程 / 去回程分别拆段 的总价对比 */
  function renderStrategies(data) {
    var rows = arr(data.strategies).filter(isObj);
    if (!rows.length) return null;
    var currency = queryCurrency(data);
    var section = el('section', { class: 'panel strategy-panel', 'aria-labelledby': 'strategy-title' }, [
      el('div', { class: 'section-head' }, [
        el('h2', { id: 'strategy-title', class: 'section-title' }, '往返购买方式对比'),
        el('span', { class: 'section-sub' }, '一张往返票 / 两张单程票 / 去回程分别拆段，按总价从低到高')
      ])
    ]);
    var table = el('table', { class: 'strategy-table' });
    var thead = el('thead', {}, [
      el('tr', {}, [
        el('th', {}, '购买方式'),
        el('th', { class: 'num' }, '总价'),
        el('th', { class: 'num' }, '比最省多花'),
        el('th', { class: 'num' }, '全程时长'),
        el('th', {}, '说明')
      ])
    ]);
    table.appendChild(thead);
    var tbody = el('tbody');
    rows.forEach(function (s, i) {
      var isBest = i === 0;
      var labelCell = el('td', {}, [el('span', {}, str(s.label, ''))]);
      if (isBest) labelCell.appendChild(el('span', { class: 'tag tag-cheapest' }, '最省'));
      tbody.appendChild(el('tr', { class: isBest ? 'is-best' : '' }, [
        labelCell,
        el('td', { class: 'num' }, fmtMoney(num(s.price, 0), currency)),
        el('td', { class: 'num' }, isBest ? '—' : fmtMoney(num(s.delta_vs_cheapest, 0), currency)),
        el('td', { class: 'num' }, fmtDuration(num(s.duration_min, 0))),
        el('td', { class: 'muted' }, str(s.note, ''))
      ]));
    });
    table.appendChild(tbody);
    section.appendChild(table);
    return section;
  }

  function renderPlans(data, options) {
    var opts = options || {};
    var title = opts.title || '拆分方案对比';
    var subtitle = opts.subtitle ||
      ('共 ' + data.plans.length + ' 个候选枢纽 · 点击「全部经济舱 / 建议组合 / 全部公务舱」可切换高亮');
    var section = el('section', { class: 'plans-section', 'aria-labelledby': 'plans-title' }, [
      el('div', { class: 'section-head' }, [
        el('h2', { id: opts.titleId || 'plans-title', class: 'section-title' }, title),
        el('span', { class: 'section-sub' }, subtitle)
      ])
    ]);
    var grid = el('div', { class: 'plans' });
    data.plans.forEach(function (plan, index) {
      var card = renderPlanCard(plan, index, data);
      /* 记下这张卡属于哪个 data / 哪组 plans，往返模式下两组卡片互不串 */
      card.__plansRef = { data: data, plans: data.plans };
      grid.appendChild(card);
    });
    section.appendChild(grid);
    return section;
  }

  function renderPlanCard(plan, index, data) {
    var card = el('article', {
      class: 'panel plan-card',
      'data-plan-index': String(index),
      'aria-label': '方案 ' + (index + 1) + '：' + plan.label
    });

    var ecoCombo = findComboByCabins(plan, plan.legs.map(function () { return 'ECONOMY'; }));
    var subParts = ['枢纽 ' + plan.hub_name + (plan.hub ? ' ' + plan.hub : '')];
    if (plan.layovers_min.length) {
      subParts.push(plan.layovers_min.map(function (minutes) {
        return '中转 ' + fmtDuration(minutes);
      }).join(' + '));
    }
    if (ecoCombo) {
      subParts.push('全经济舱 ' + fmtMoney(ecoCombo.total_price, ecoCombo.currency));
      subParts.push('总时长 ' + fmtDuration(ecoCombo.total_duration_min));
    }

    card.appendChild(el('header', { class: 'plan-head' }, [
      el('div', { class: 'hub-avatar', 'aria-hidden': 'true' }, plan.hub || 'HUB'),
      el('div', { class: 'plan-head-main' }, [
        el('h3', { class: 'plan-title' }, plan.label),
        el('p', { class: 'plan-sub' }, subParts.join(' · '))
      ])
    ]));

    if (plan.note) {
      card.appendChild(el('p', { class: 'plan-note' }, '注意：' + plan.note));
    }

    card.appendChild(renderCompare(plan, data));

    card.appendChild(el('div', { class: 'section-head legs-head' }, [
      el('h4', { class: 'section-title small' }, '分段明细（每段单独买票）'),
      el('span', { class: 'section-sub' }, '每段的经济舱 / 公务舱报价、升舱差价与「每多睡 1 小时」成本')
    ]));
    card.appendChild(renderLegs(plan, data));

    var bestValue = renderBestValue(plan);
    if (bestValue) card.appendChild(bestValue);

    var allCombos = renderAllCombos(plan);
    if (allCombos) card.appendChild(allCombos);
    return card;
  }

  function renderCompare(plan, data) {
    var rows = buildPresetRows(plan, data);
    var maxPrice = 0;
    var maxRest = 0;
    rows.forEach(function (row) {
      maxPrice = Math.max(maxPrice, num(row.combo.total_price, 0));
      maxRest = Math.max(maxRest, num(row.combo.comfort_score, 0));
    });

    var wrap = el('div', { class: 'compare' }, [
      el('div', { class: 'compare-head' }, [
        el('h4', { class: 'compare-title' }, '成本 × 休息收益'),
        el('span', { class: 'legend' }, [
          el('i', { class: 'dot dot-price', 'aria-hidden': 'true' }),
          '总价',
          el('i', { class: 'dot dot-rest', 'aria-hidden': 'true' }),
          '有效休息（小时）'
        ])
      ]),
      el('p', { class: 'compare-hint' },
        '有效休息 = 各段按舱位折算的可睡眠小时数之和（≈0.15 + 0.85×夜间占比，再乘舱位舒适度）。' +
        '条越长 = 越贵 / 休息越多；同一方案内三条可直接横向比较。')
    ]);

    if (!rows.length) {
      wrap.appendChild(el('p', { class: 'compare-empty' }, '该方案缺少舱位组合数据，无法做成本 × 休息对比。'));
    } else {
      var list = el('div', {
        class: 'combo-list',
        role: 'group',
        'aria-label': '三种舱位组合的成本与休息对比'
      });
      rows.forEach(function (row) {
        list.appendChild(renderComboRow(row, plan, maxPrice, maxRest, data));
      });
      wrap.appendChild(list);
      wrap.appendChild(el('div', { class: 'plan-detail', 'aria-live': 'polite' }));
    }

    var baseline = data.baseline || {};
    var baselineParts = [];
    function pushBaseline(label, item) {
      if (!isObj(item)) return;
      var stopsText = '';
      if (isNum(item.stops)) stopsText = item.stops > 0 ? ' · ' + item.stops + '次中转' : ' · 直飞';
      baselineParts.push(label + ' ' + fmtMoney(item.price, queryCurrency(data)) +
        (isNum(item.duration_min) ? '（' + fmtDuration(item.duration_min) + '）' : '') + stopsText);
    }
    pushBaseline('最低价经济舱', baseline.through_economy);
    pushBaseline('最低价公务舱', baseline.through_business);
    pushBaseline('直飞经济舱', baseline.nonstop_economy);
    pushBaseline('直飞公务舱', baseline.nonstop_business);
    if (baselineParts.length) {
      wrap.appendChild(el('p', { class: 'baseline' }, '同日期整程票参考：' + baselineParts.join(' · ')));
    }
    if (data.direct_note) {
      wrap.appendChild(el('p', { class: 'baseline' }, '直飞对比：' + data.direct_note));
    }
    return wrap;
  }

  function renderComboRow(row, plan, maxPrice, maxRest, data) {
    var combo = row.combo;
    var currency = combo.currency || queryCurrency(data);
    var pricePct = maxPrice > 0 ? clamp(num(combo.total_price, 0) / maxPrice * 100, 3, 100) : 0;
    var restPct = maxRest > 0 ? clamp(num(combo.comfort_score, 0) / maxRest * 100, 0, 100) : 0;

    var tags = [];
    if (row.isRecommended) tags.push(el('span', { class: 'tag tag-rec' }, '建议'));
    if (row.isPareto) tags.push(el('span', { class: 'tag tag-pareto' }, '帕累托'));
    if (row.presets.some(function (preset) { return preset.key === 'economy'; })) {
      tags.push(el('span', { class: 'tag tag-eco' }, '最低价'));
    }
    if (row.presets.some(function (preset) { return preset.key === 'business'; })) {
      tags.push(el('span', { class: 'tag tag-biz' }, '休息最多'));
    }

    return el('button', {
      type: 'button',
      class: 'combo-row',
      'data-row-key': row.key,
      'aria-pressed': 'false',
      title: '点击高亮：' + combo.cabins_cn.join(' + ')
    }, [
      el('span', { class: 'combo-name' }, [
        el('b', {}, row.presets[0] ? row.presets[0].label : '组合'),
        el('small', {}, combo.cabins_cn.join(' + '))
      ]),
      el('span', { class: 'combo-tags' }, tags),
      el('span', { class: 'combo-bars' }, [
        comboBarRow('总价', fmtMoney(combo.total_price, currency), pricePct, 'price'),
        comboBarRow('有效休息', fmtHours(combo.comfort_score), restPct, 'rest')
      ]),
      el('span', { class: 'combo-delta' },
        '比全经济舱 ' + fmtDeltaMoney(combo.price_delta, currency) + ' · ' + fmtDeltaHours(combo.rest_gain) + ' 休息')
    ]);
  }

  function comboBarRow(label, valueText, percent, kind) {
    var fill = el('i', { class: 'bar-fill bar-' + kind });
    fill.style.width = (Math.round(percent * 10) / 10) + '%';
    return el('span', { class: 'bar-row' }, [
      el('span', { class: 'bar-label' }, label),
      el('span', { class: 'bar-track', 'aria-hidden': 'true' }, fill),
      el('span', { class: 'bar-value' }, valueText)
    ]);
  }

  function comboDetailContent(plan, combo, data) {
    if (!combo) {
      return [el('span', { class: 'detail-empty' }, '该方案缺少组合数据，暂无法展示。')];
    }
    var currency = combo.currency || queryCurrency(data);
    var items = [
      el('span', { class: 'detail-kicker' }, '已选组合'),
      el('b', { class: 'detail-cabins' }, combo.cabins_cn.join(' + ')),
      el('span', { class: 'detail-item' }, '总价 ' + fmtMoney(combo.total_price, currency)),
      el('span', { class: 'detail-item' }, '总时长 ' + fmtDuration(combo.total_duration_min) +
        '（地面 ' + fmtDuration(combo.total_ground_min) + '）'),
      el('span', { class: 'detail-item' }, '有效休息 ' + fmtHours(combo.comfort_score)),
      el('span', { class: 'detail-delta' }, '比全经济舱 ' +
        fmtDeltaMoney(combo.price_delta, currency) + ' · ' + fmtDeltaHours(combo.rest_gain) + ' 休息'),
      el('span', { class: 'detail-value' }, combo.value_per_rest_hour == null
        ? '每多睡 1 小时：—（没有增加夜间休息）'
        : '每多睡 1 小时：' + fmtMoney(combo.value_per_rest_hour, currency))
    ];

    var baseline = data.baseline || {};
    var allEconomy = combo.cabins.every(function (cabin) { return cabin === 'ECONOMY'; });
    var reference = allEconomy ? baseline.through_economy : baseline.through_business;
    var referenceLabel = allEconomy ? '整程经济舱' : '整程公务舱';
    if (isObj(reference) && isNum(parseFloat(reference.price))) {
      var diff = parseFloat(reference.price) - num(combo.total_price, 0);
      var text;
      if (Math.abs(diff) < 0.5) text = '与' + referenceLabel + '同价';
      else text = (diff > 0 ? '比' + referenceLabel + '省 ' : '比' + referenceLabel + '贵 ') + fmtMoney(Math.abs(diff), currency);
      items.push(el('span', { class: 'detail-baseline' }, text));
    }
    return items;
  }

  function renderLegs(plan, data) {
    var wrap = el('div', { class: 'legs' });
    plan.legs.forEach(function (leg, index) {
      if (index > 0) {
        var layover = plan.layovers_min[index - 1];
        wrap.appendChild(el('div', { class: 'layover' }, [
          el('span', { class: 'layover-line', 'aria-hidden': 'true' }),
          el('span', { class: 'layover-text' },
            '中转等待 ' + fmtDuration(layover) + ' · ' + plan.hub_name + '（同一机场）'),
          el('span', { class: 'layover-line', 'aria-hidden': 'true' })
        ]));
      }
      wrap.appendChild(renderLeg(leg, plan, data, index));
    });
    return wrap;
  }

  function renderLeg(leg, plan, data, position) {
    var legNo = num(position, num(leg.index, 0));
    var anyOption = optionFor(leg, 'ECONOMY') || optionFor(leg, 'BUSINESS');
    var cabinKeys = Object.keys(leg.options || {});
    if (!anyOption && cabinKeys.length) anyOption = leg.options[cabinKeys[0]];

    var economy = optionFor(leg, 'ECONOMY');
    var business = optionFor(leg, 'BUSINESS');
    var otherCabins = cabinKeys.filter(function (cabin) {
      return cabin !== 'ECONOMY' && cabin !== 'BUSINESS';
    });

    var node = el('div', { class: 'leg', 'data-leg-index': String(legNo) });

    var flightText = '';
    if (anyOption) {
      var segment = arr(anyOption.segments)[0];
      var flightNo = segment && segment.flight_no ? segment.flight_no : (anyOption.carrier || '');
      var stops = numOr(anyOption.stops, 0);
      flightText = [flightNo, stops > 0 ? '经停/中转 ' + stops + ' 次' : '直飞',
        fmtDuration(legDuration(leg))].filter(Boolean).join(' · ');
    }

    node.appendChild(el('div', { class: 'leg-head' }, [
      el('span', { class: 'leg-no' }, '第 ' + (legNo + 1) + ' 段'),
      el('span', { class: 'leg-route' }, [
        el('b', {}, cityName(leg.origin, plan, data)),
        el('span', { class: 'code' }, leg.origin),
        el('span', { class: 'arrow' }, '→'),
        el('b', {}, cityName(leg.destination, plan, data)),
        el('span', { class: 'code' }, leg.destination)
      ]),
      el('span', { class: 'leg-cabin-badge', 'aria-live': 'polite' })
    ]));

    node.appendChild(el('p', { class: 'leg-sub' }, flightText || '航班信息缺失'));

    node.appendChild(el('div', { class: 'leg-times' }, [
      el('span', {}, [
        el('b', {}, '起飞 '),
        fmtLocalDateTime(anyOption ? anyOption.depart_local : '')
      ]),
      el('span', { class: 'arrow' }, '→'),
      el('span', {}, [
        el('b', {}, '到达 '),
        fmtLocalDateTime(anyOption ? anyOption.arrive_local : '')
      ]),
      el('span', { class: 'tz-note' },
        '均为当地时间 · 时差 ' + (leg.tz_shift_h >= 0 ? '+' : '') + leg.tz_shift_h + ' 小时')
    ]));

    var ecoPrice = economy ? numOr(economy.price, null) : null;
    var bizPrice = business ? numOr(business.price, null) : null;
    var currency = (economy && economy.currency) || (business && business.currency) || queryCurrency(data);

    var prices = el('div', { class: 'leg-prices' });
    prices.appendChild(priceCell('经济舱价', ecoPrice == null ? '—' : fmtMoney(ecoPrice, currency), 'ECONOMY',
      ecoPrice == null ? '该段无经济舱报价' : ''));
    prices.appendChild(priceCell('公务舱价', bizPrice == null ? '—' : fmtMoney(bizPrice, currency), 'BUSINESS',
      bizPrice == null ? '该段无公务舱报价' : ''));

    if (ecoPrice != null && bizPrice != null) {
      prices.appendChild(priceCell('升舱多花', fmtMoney(bizPrice - ecoPrice, currency), 'UPGRADE', ''));
      var restEco = legRestHours(leg, 'ECONOMY');
      var restBiz = legRestHours(leg, 'BUSINESS');
      var extraRest = (restEco != null && restBiz != null) ? (restBiz - restEco) : null;
      var perHour = extraRest != null && extraRest > 0.05 ? (bizPrice - ecoPrice) / extraRest : null;
      var note;
      if (perHour != null) note = '多睡 ' + fmtHours(extraRest);
      else if (extraRest != null) note = '白天航班，升舱不增加夜间休息';
      else note = '休息数据缺失';
      prices.appendChild(priceCell('每多睡 1 小时', perHour == null ? '—' : fmtMoney(perHour, currency),
        'PERHOUR', note));
    } else {
      prices.appendChild(priceCell('升舱多花', '—', 'UPGRADE', '缺少对比报价'));
      prices.appendChild(priceCell('每多睡 1 小时', '—', 'PERHOUR', '缺少对比报价'));
    }
    node.appendChild(prices);

    if (otherCabins.length) {
      node.appendChild(el('p', { class: 'leg-other' }, '其他舱位：' + otherCabins.map(function (cabin) {
        var option = leg.options[cabin];
        return cabinCn(cabin) + ' ' + fmtMoney(option.price, option.currency || currency);
      }).join(' · ')));
    }

    var rest = leg.rest;
    if (rest && !rest.missing) {
      node.appendChild(el('p', { class: 'leg-rest' }, [
        el('span', {}, '有效休息：经济舱 ' + fmtHours(legRestHours(leg, 'ECONOMY')) +
          ' / 公务舱 ' + fmtHours(legRestHours(leg, 'BUSINESS'))),
        el('span', {}, '睡眠窗口 ' + fmtHours(rest.sleep_h) +
          ' · 夜间占比 ' + fmtPercent(rest.night_ratio) +
          ' · 休息分 ' + fmtScore(rest.score)),
        rest.verdict ? el('em', {}, rest.verdict) : null
      ]));
      /* api.py 在「经济舱 / 公务舱不是同一班次」时会给出提醒，避免把换班次的钱算成纯升舱 */
      if (rest.upgrade_note) {
        var upgradeNote = rest.upgrade_note.replace(/(\d{2}-\d{2})T(\d{2}:\d{2})/g, '$1 $2');
        node.appendChild(el('p', { class: 'leg-upgrade-note' }, '⚠ ' + upgradeNote));
      }
    } else {
      node.appendChild(el('p', { class: 'leg-rest missing' }, '该段缺少休息评分数据（rest），无法计算升舱的休息收益。'));
    }
    return node;
  }

  function priceCell(label, value, cabin, note) {
    var cell = el('div', { class: 'price-cell', 'data-cabin': cabin }, [
      el('span', { class: 'price-label' }, label),
      el('span', { class: 'price-value' }, value)
    ]);
    if (note) cell.appendChild(el('span', { class: 'price-note' }, note));
    return cell;
  }

  function legRestHours(leg, cabin) {
    if (!leg || !leg.rest || leg.rest.missing) return null;
    var value = leg.rest.effective_rest_h ? leg.rest.effective_rest_h[cabin] : null;
    return value != null && isFinite(value) ? value : null;
  }

  function cityName(code, plan, data) {
    var query = (data && data.query) || {};
    var key = str(code, '');
    if (query.origin && key === query.origin && query.origin_name) return query.origin_name;
    if (query.destination && key === query.destination && query.destination_name) return query.destination_name;
    if (plan && plan.hub && key === plan.hub && plan.hub_name) return plan.hub_name;
    return key || '未知';
  }

  function renderBestValue(plan) {
    var best = plan.best_value;
    if (!isObj(best)) return null;
    var leg = plan.legs[best.leg_index];
    var legName = leg
      ? ('第 ' + (best.leg_index + 1) + ' 段（' + leg.origin + ' → ' + leg.destination + '）')
      : ('第 ' + (best.leg_index + 1) + ' 段');

    var parts = [];
    if (best.extra_cost != null) parts.push('多花 ' + fmtMoney(best.extra_cost));
    if (best.extra_rest_h != null) parts.push('多睡 ' + fmtHours(best.extra_rest_h));
    if (best.cost_per_rest_h != null) parts.push('约 ' + fmtMoney(best.cost_per_rest_h) + ' / 小时');

    return el('div', { class: 'best-value' }, [
      el('span', { class: 'best-icon', 'aria-hidden': 'true' }, '💡'),
      el('span', {}, '升舱性价比最高：把' + legName + '升到' + cabinCn(best.cabin) +
        (parts.length ? '，' + parts.join('，') : '') + '。')
    ]);
  }

  function renderAllCombos(plan) {
    if (!plan.combos.length) return null;
    var details = el('details', { class: 'all-combos' });
    details.appendChild(el('summary', {}, '查看全部 ' + plan.combos.length + ' 种舱位组合（含混搭）'));

    var table = el('table', { class: 'combo-table' });
    var head = el('thead');
    head.appendChild(el('tr', {}, [
      el('th', {}, '舱位组合'),
      el('th', {}, '总价'),
      el('th', {}, '总时长'),
      el('th', {}, '有效休息'),
      el('th', {}, '比全经济舱'),
      el('th', {}, '每多睡 1 小时'),
      el('th', {}, '标签')
    ]));
    table.appendChild(head);

    var recCombo = findRecommendedCombo(plan, state.data || {});
    var recSig = recCombo ? comboSignature(recCombo) : '';
    var pareto = arr(plan.pareto);
    var body = el('tbody');
    plan.combos.forEach(function (combo, index) {
      var tags = [];
      if (pareto.indexOf(index) >= 0) tags.push('帕累托');
      if (recSig && comboSignature(combo) === recSig) tags.push('推荐');
      body.appendChild(el('tr', {}, [
        el('td', {}, combo.cabins_cn.join(' + ')),
        el('td', { class: 'num' }, fmtMoney(combo.total_price, combo.currency)),
        el('td', { class: 'num' }, fmtDuration(combo.total_duration_min)),
        el('td', { class: 'num' }, fmtHours(combo.comfort_score)),
        el('td', { class: 'num' }, fmtDeltaMoney(combo.price_delta, combo.currency) + ' / ' + fmtDeltaHours(combo.rest_gain)),
        el('td', { class: 'num' }, combo.value_per_rest_hour == null ? '—' : fmtMoney(combo.value_per_rest_hour, combo.currency)),
        el('td', {}, tags.join(' · '))
      ]));
    });
    table.appendChild(body);
    details.appendChild(el('div', { class: 'table-scroll' }, table));
    return details;
  }

  function renderWarnings(data) {
    /* api.py 的 recommend() 已按 SPEC §5.5 输出完整警告；顶层 warnings 往往是同义重复，
       因此优先用 recommendation.warnings，只有它缺失时才回退顶层 warnings。 */
    var recWarnings = uniqueStrings(arr(data.recommendation && data.recommendation.warnings));
    var topWarnings = uniqueStrings(arr(data.warnings));
    var warnings = recWarnings.length ? recWarnings : topWarnings;
    if (!warnings.length) return null;
    return el('section', { class: 'panel warnings', 'aria-labelledby': 'warnings-title' }, [
      el('h2', { id: 'warnings-title', class: 'warnings-title' }, '⚠ 风险提示（比价格更值得先看）'),
      el('ul', { class: 'warnings-list' }, warnings.map(function (warning) {
        return el('li', {}, warning);
      })),
      el('p', { class: 'warnings-foot' },
        '以上提示来自后端 recommend().warnings（缺失时回退 warnings 字段），请在出票前逐条核对。')
    ]);
  }

  function renderMeta(data, meta) {
    var query = data.query || {};
    var parts = [];
    if (query.origin_name || query.origin || query.destination_name || query.destination) {
      parts.push('航线 ' + (query.origin_name || query.origin || '?') + ' → ' +
        (query.destination_name || query.destination || '?'));
    }
    if (query.date) {
      parts.push('出发日期 ' + query.date +
        (num(query.date_window, 0) > 0 ? '（±' + num(query.date_window, 0) + ' 天窗口）' : ''));
    }
    if (query.adults) parts.push('成人 ' + num(query.adults, 1) + ' 人');
    if (query.currency) parts.push('币种 ' + query.currency);
    if (data.generated_at) parts.push('生成时间 ' + fmtLocalDateTime(data.generated_at));
    if (data.source && data.source.name) {
      parts.push('来源 ' + data.source.name + (data.source.live ? '（实时）' : '（非实时）'));
    }
    if (meta && meta.sample) parts.push('当前为示例数据');
    parts.push('价格为单人含税参考价');

    return el('p', { class: 'meta-line' }, parts.join(' · '));
  }

  function renderEmpty(data, meta) {
    var rec = data.recommendation || {};
    var section = el('section', { class: 'panel notice empty', role: 'status' }, [
      el('h2', {}, '没有找到可行的拆分方案'),
      el('p', { class: 'notice-lead' },
        rec.headline || '后端返回了 0 个拆分方案；可以调整条件后重试。'),
      data.errors.length
        ? el('ul', { class: 'notice-list' }, data.errors.map(function (error) { return el('li', {}, error); }))
        : null,
      el('p', { class: 'notice-hint' },
        '可以试试：换一个出发日期、增加候选枢纽（如 IST,DOH,DXB）、放宽日期窗口，' +
        (meta && meta.sample ? '或者连上后端后再查询。' : '或者打开「离线演示」查看示例数据。'))
    ]);
    section.appendChild(el('div', { class: 'notice-actions' }, [
      el('button', { type: 'button', class: 'btn primary', 'data-action': 'retry' }, '重新查询'),
      el('button', { type: 'button', class: 'btn ghost', 'data-action': 'load-sample' }, '加载示例数据')
    ]));
    return section;
  }

  function renderFatal(message, hint) {
    var section = el('section', { class: 'panel notice error', role: 'alert' }, [
      el('h2', {}, '数据加载失败（页面已降级，不会白屏）'),
      el('p', { class: 'notice-lead' }, message || '未知错误'),
      hint ? el('p', { class: 'notice-hint' }, hint) : null,
      el('ul', { class: 'notice-list' }, [
        el('li', {}, '确认后端已启动：python -m flightwatch serve。'),
        el('li', {}, '纯静态自测：python -m http.server 8899 -d flightwatch/web，再访问 http://127.0.0.1:8899/。'),
        el('li', {}, '确认 flightwatch/web/sample_plan.json 存在且是合法 JSON。')
      ]),
      el('div', { class: 'notice-actions' }, [
        el('button', { type: 'button', class: 'btn primary', 'data-action': 'retry' }, '重新查询'),
        el('button', { type: 'button', class: 'btn ghost', 'data-action': 'load-sample' }, '加载示例数据')
      ])
    ]);
    if (dom.results) dom.results.replaceChildren(section);
    else return section;
  }

  function showRuntimeError(message) {
    if (!dom.appError) return;
    dom.appError.classList.remove('hidden');
    dom.appError.replaceChildren(el('p', {},
      '页面出现异常：' + message + '。可以重新查询，或勾选「离线演示」查看示例数据。'));
    setStatus('页面出现异常，已降级显示。', 'error');
  }

  /* ------------------------------------------------------------------ */
  /* 交互：组合切换高亮 / 按钮动作                                       */
  /* ------------------------------------------------------------------ */

  function onResultsClick(event) {
    var target = event.target;
    if (!target || !target.closest) return;

    var actionEl = target.closest('[data-action]');
    if (actionEl) {
      handleAction(actionEl.getAttribute('data-action'));
      return;
    }

    var rowEl = target.closest('[data-row-key]');
    if (!rowEl) return;
    var card = rowEl.closest('.plan-card');
    if (!card) return;
    var plan = state.data && state.data.plans[num(card.getAttribute('data-plan-index'), -1)];
    if (!plan) return;
    state.selection[planKey(plan)] = rowEl.getAttribute('data-row-key');
    applyPlanSelection(card, plan, state.data);
  }

  function handleAction(action) {
    if (action === 'retry') {
      runQuery();
      return;
    }
    if (action === 'load-sample') {
      loadSample('手动加载本地示例数据');
      return;
    }
    if (action === 'retry-live') {
      if (dom.offline) dom.offline.checked = false;
      runQuery();
    }
  }

  function applyAllSelections(data) {
    if (!dom.results) return;
    $all('.plan-card', dom.results).forEach(function (card) {
      var ref = card.__plansRef;
      var plans = ref ? ref.plans : data.plans;
      var dataRef = ref ? ref.data : data;
      var plan = plans[num(card.getAttribute('data-plan-index'), -1)];
      if (plan) applyPlanSelection(card, plan, dataRef);
    });
  }

  function applyPlanSelection(card, plan, data) {
    var rows = buildPresetRows(plan, data);
    var wanted = state.selection[planKey(plan)] || defaultPresetKey(plan, data);
    var row = pickRow(rows, wanted);
    var rowKey = row ? row.key : null;

    $all('[data-row-key]', card).forEach(function (button) {
      var selected = button.getAttribute('data-row-key') === rowKey;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });

    var detail = card.querySelector('.plan-detail');
    if (detail) {
      detail.replaceChildren.apply(detail, comboDetailContent(plan, row ? row.combo : null, data));
    }
    highlightLegs(card, row ? row.combo : null);
  }

  function highlightLegs(card, combo) {
    var cabins = combo && Array.isArray(combo.cabins) ? combo.cabins : [];
    $all('.leg', card).forEach(function (legEl) {
      var index = num(legEl.getAttribute('data-leg-index'), -1);
      var cabin = cabins[index] || 'ECONOMY';
      var upgraded = cabin !== 'ECONOMY';
      legEl.classList.toggle('upgraded', upgraded);
      var badge = legEl.querySelector('.leg-cabin-badge');
      if (badge) {
        badge.textContent = upgraded ? '已选 ' + cabinCn(cabin) : '';
      }
      $all('[data-cabin]', legEl).forEach(function (cell) {
        var cellCabin = cell.getAttribute('data-cabin');
        cell.classList.toggle('active', cellCabin === cabin);
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* 导出（浏览器用 window.FlightWatchUI；node 自检用 module.exports）   */
  /* ------------------------------------------------------------------ */

  return {
    CONFIG: CONFIG,
    init: init,
    state: state,
    buildQuery: buildQuery,
    buildApiUrl: buildApiUrl,
    parseHubs: parseHubs,
    normalizeData: normalizeData,
    normalizePlan: normalizePlan,
    synthCombos: synthCombos,
    getPresets: getPresets,
    buildPresetRows: buildPresetRows,
    findRecommendedCombo: findRecommendedCombo,
    defaultPresetKey: defaultPresetKey,
    pickRow: pickRow,
    comboSignature: comboSignature,
    legRestHours: legRestHours,
    formatDuration: fmtDuration,
    formatMoney: fmtMoney,
    formatHours: fmtHours,
    formatLocalDateTime: fmtLocalDateTime
  };
});
