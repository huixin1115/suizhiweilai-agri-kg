/* ==================================================================
   物候提醒 · 详情渲染器（被「物候期提醒」页与「详情页」共用）
   ------------------------------------------------------------------
   对外只暴露三个函数：
       esc(s)                      HTML 转义
       renderTimeline(stages, i)   生育期时间轴
       renderReminder(data)        整份「今日提醒」的 HTML
   依赖的样式类：.sect .line .okbar .warnbar .hint .empty .tag
                 .bar .timeline .tl-seg .timeline-cap 以及 table 默认样式
   ================================================================== */
'use strict';

function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ---------- 生育期主题：每类时期一套配色，点开后一眼能认出是哪一类 ---------- */
const STAGE_THEMES = [
  { keys: ['播种','出苗','育苗','萌芽','移栽','定植','缓苗','直播'],
    kind: '播种出苗', grad: 'linear-gradient(135deg,#d8ecc9,#7fb069)', accent: '#5f9a4e', tint: '#f2f9ee' },
  { keys: ['苗期','幼苗','分蘖','分枝','发棵','莲座','团棵','伸蔓','抽梢','幼龄','营养生长'],
    kind: '营养生长', grad: 'linear-gradient(135deg,#b6dca6,#3f8f4a)', accent: '#3f8f4a', tint: '#f0f8ee' },
  { keys: ['拔节','蕾','花芽','现蕾','孕穗','抽穗','开花','授粉','花铃','生殖生长'],
    kind: '开花生殖', grad: 'linear-gradient(135deg,#f7dc93,#c99a2e)', accent: '#c08b18', tint: '#fdf8ea' },
  { keys: ['灌浆','结荚','鼓粒','膨大','结球','块茎','块根','淀粉','角果','果实','坐果','浆果','糖分','根茎'],
    kind: '产品形成', grad: 'linear-gradient(135deg,#f0c45f,#b8830f)', accent: '#b8830f', tint: '#fdf6e8' },
  { keys: ['成熟','收获','采收','采摘','吐絮','转色','着色','休眠','落叶','越冬','采挖'],
    kind: '成熟收获', grad: 'linear-gradient(135deg,#dcac6b,#8f5f26)', accent: '#9a6a2c', tint: '#fbf4ea' }
];
const STAGE_THEME_FALLBACK = { kind: '生育期', grad: 'linear-gradient(135deg,#9fc3e8,#3f7d5f)', accent: '#2F5597', tint: '#f4f8fd' };

function stageTheme(short){
  const s = String(short || '');
  for(const t of STAGE_THEMES){
    if(t.keys.some(k => s.includes(k))) return t;
  }
  return STAGE_THEME_FALLBACK;
}

/* 生育期时间轴：方块宽度按历时比例，深色为当前阶段。
   selIdx 不为空时表示"正在查看某个历史/未来阶段"，此时给出醒目的返回入口。 */
function renderTimeline(stages, curIdx, selIdx){
  if(!stages || !stages.length) return '';
  let h = '<div class="timeline">';
  stages.forEach((s, i) => {
    const cls = i < curIdx ? 'done' : (i === curIdx ? 'cur' : (i === curIdx + 1 ? 'next' : ''));
    const sel = (selIdx !== undefined && selIdx !== null && Number(selIdx) === i) ? ' sel' : '';
    h += `<div class="tl-seg tl-click ${cls}${sel}" data-idx="${i}" role="button" tabindex="0"
            title="点开看「${esc(s.short)}」的农事与风险（第 ${s.start}-${s.end} 天，历时 ${s.days} 天）">
      ${esc(s.short)}<br><span style="opacity:.72">${s.start}-${s.end}天</span></div>`;
  });
  h += '</div>';
  const viewing = (selIdx !== undefined && selIdx !== null && Number(selIdx) >= 0);
  if(viewing){
    h += `<div class="timeline-cap viewing">
      正在查看：<b>${esc(stages[Number(selIdx)].short)}</b>（第 ${Number(selIdx) + 1} / ${stages.length} 个阶段，下面内容已切换）
      ${window.BROWSE_MODE ? '' : '<button type="button" class="tl-back" data-back="1">← 回到当前阶段</button>'}
    </div>`;
  } else {
    h += `<div class="timeline-cap">${window.BROWSE_MODE
       ? '方块宽度按各阶段历时比例　·　<b>点任意方块</b>即可查看该时期的农事与风险'
       : '方块宽度按各阶段历时比例；深色为当前阶段，虚线框为下一阶段　·　<b>点任意方块</b>即可把下面内容切换成该时期'}</div>`;
  }
  return h;
}

/* 单个生育期的资料（时间轴上选中某一段后，用它替换正文）
   showHero=false 时不再重复出图 —— 此时页面顶部大图已切成该时期的照片 */
function renderStagePack(d, showHero){
  if(!d || d.error) return `<div class="empty">${esc((d && d.error) || '加载失败')}</div>`;
  const s = d.stage || {};
  const th = stageTheme(s.short);
  const total = d.stage_count || 0;
  const i = d.stage_index;
  if(showHero === undefined) showHero = true;

  /* —— 阶段定位：跟"当前阶段"的关系，避免切来切去看不清自己在哪一段 —— */
  let rel = '当前所处阶段';
  let relCls = 'now';
  if(window.BROWSE_MODE){
    rel = `第 ${i + 1} / ${total || '?'} 个生育期`;
    relCls = 'past';
  } else if(d.cur_index !== undefined && d.cur_index !== null && d.cur_index >= 0 && !d.is_current){
    const diff = i - d.cur_index;
    rel = diff < 0 ? `比当前阶段早 ${-diff} 段` : `比当前阶段晚 ${diff} 段`;
    relCls = diff < 0 ? 'past' : 'future';
  }

  let h = `<div class="spack-top" style="--ac:${th.accent};background:${th.tint}">
      <div class="spack-badge">${esc(th.kind)}　·　第 ${i + 1} / ${total} 个阶段</div>
      <h3 class="spack-name">${esc(s.short)}</h3>
      <div class="spack-meta">
        <span>第 ${d.start}–${d.end} 天</span>
        <span>历时 ${d.days} 天</span>
        <span class="spack-rel ${relCls}">${esc(rel)}</span>
      </div>
    </div>`;

  if(showHero && d.image){
    h += `<div class="spack-hero" style="background-image:url('${d.image}')">
            <div class="spack-imgcap">${esc(d.crop || '')} · ${esc(s.short)} 实拍参考</div>
          </div>`;
  }

  const todo = d.todo || [];
  h += `<div class="sect"><h3>${esc(s.short)}的农事操作</h3>`;
  h += todo.length
    ? `<table><tr><th>农事操作</th><th>适宜时期</th><th>作用</th></tr>` +
      todo.map(p => `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.timing || '-')}</td><td>${esc(p.effect || '-')}</td></tr>`).join('') + `</table>`
    : '<div class="empty">该阶段暂无具体农事条目</div>';
  h += `</div>`;

  h += fertBlock(d.fertilize);

  const risks = d.risks || [];
  h += `<div class="sect"><h3>该阶段病虫害与草害风险${risks.length ? '（' + risks.length + ' 项）' : ''}</h3>`;
  if(!risks.length){
    h += `<div class="okbar">图谱里这个阶段没有标记为高发的病虫害，保持田间巡查即可。</div>`;
  } else {
    h += `<table><tr><th>名称</th><th>类别</th><th>危害等级</th><th>危害部位</th><th>常发时期</th><th>防治措施</th></tr>` +
      risks.map(r => {
        const lv = r.level === '重' ? 'tag red' : (r.level === '中' ? 'tag amber' : 'tag gray');
        return `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.label)}</td>
          <td><span class="${lv}">${esc(r.level || '未分级')}</span></td>
          <td>${esc(r.part || '-')}</td><td>${esc(r.period || '-')}</td>
          <td>${(r.measures || []).map(m => `<span class="tag green">${esc(m)}</span>`).join('') || '-'}</td></tr>`;
      }).join('') + `</table>`;
    h += `<div class="hint">防治顺序遵循「农业防治 → 生物防治 → 物理防治 → 化学农药」；使用化学农药请严格执行安全间隔期，采收前禁用。</div>`;
  }
  h += `</div>`;

  /* —— 相邻阶段翻页：让"看不同时期"像翻页，而不是反复回到同一块面板 —— */
  const prev = d.prev_stage, next = d.next_stage;
  h += `<div class="spack-nav">
      ${prev ? `<button type="button" class="spnav" data-goto="${i - 1}">‹ 上一阶段：${esc(prev.short)}</button>` : '<span></span>'}
      <span class="spnav-idx">第 ${i + 1} / ${total} 个阶段</span>
      ${next ? `<button type="button" class="spnav" data-goto="${i + 1}">下一阶段：${esc(next.short)} ›</button>` : '<span></span>'}
    </div>`;
  return h;
}

/* 土质区块：地块固有条件，影响后面所有水肥措施
   ⚠ 这里**不显示图谱里的「酸碱度」**（那只是该土类的大致范围，跟自家地块对不上）；
   酸碱度只看农户手填的 ph_user，没填就不显示这一项、也不给酸碱类建议。 */
function soilBlock(soil){
  if(!soil) return '';
  const cls = soil.match_level === 'good' ? 'okbar' : (soil.match_level === 'warn' ? 'warnbar' : '');
  let h = `<div class="sect"><h3>土质与土壤管理 · ${esc(soil.soil)}</h3>`;
  h += `<div class="${cls || 'line'}">${esc(soil.match_text)}</div>`;
  h += `<table><tr><th>质地</th><th>肥力</th><th>保水性</th><th>主要分布</th></tr>
    <tr>
      <td>${esc(soil.texture)}${soil.texture_label && !/未标注/.test(soil.texture_label) ? '（' + esc(soil.texture_label) + '）' : ''}</td>
      <td>${esc(soil.fertility)}</td><td>${esc(soil.water)}</td><td>${esc(soil.area || '-')}</td>
    </tr></table>`;
  /* 农户自己填的土壤 pH（有才显示） */
  if(soil.ph_user !== null && soil.ph_user !== undefined && soil.ph_user !== ''){
    h += `<div class="line" style="margin-top:10px"><b>土壤 pH</b>（你自己填的）：
      <b>${esc(soil.ph_user)}</b>${soil.ph_label ? '　' + esc(soil.ph_label) : ''}</div>`;
  } else {
    h += `<div class="hint">未填土壤 pH。填了才会给调酸 / 调碱的具体建议 —— 在物候期提醒页的「添加 / 更新地块」里可以补填。</div>`;
  }
  const tips = [].concat(soil.tips || [], soil.ph_tips || [], soil.fert_tips || [], soil.water_tips || []);
  if(tips.length){
    h += `<div class="line" style="margin-top:10px"><b>针对这块地，水肥管理要这样调整</b></div>
      <ul style="margin:6px 0 0 20px;font-size:13px;line-height:1.9;color:#334155">
      ${tips.map(t => `<li>${esc(t)}</li>`).join('')}</ul>`;
  }
  if((soil.amendments || []).length){
    h += `<div class="line" style="margin-top:9px">该土壤常用改良剂：` +
      soil.amendments.map(a => `<span class="tag green">${esc(a)}</span>`).join('') + `</div>`;
  }
  return h + `</div>`;
}

/* 经济效益测算回写块：在测算页把方案关联到这块地之后，这里显示最近一次测算的结论。
   ⚠ 数据来自 crop_econ 表按 plot_id 反查的最新一条，不是另存一份 —— 改了测算，这里跟着变。
   ⚠ 只有地块详情（mode=plot）才有；作物浏览页没有地块，不显示。 */
function econBlock(econ, plotId){
  if(!econ || !plotId) return '';
  const money = v => Number(v || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  const fx = (v, n) => (v === null || v === undefined ? '-' : Number(v).toFixed(n));
  let h = `<div class="sect"><h3>经济效益测算 · ${esc(econ.crop || '')}</h3>`;
  h += `<table><tr><th>亩产</th><th>收购价</th><th>亩成本</th><th>亩均净收益</th>` +
       `<th>${econ.area_mu ? fx(econ.area_mu, 1) + ' 亩' : ''}总净收益</th></tr><tr>` +
       `<td>${fx(econ.yield_per_mu, 0)} 公斤</td><td>${fx(econ.price, 2)} 元/公斤</td>` +
       `<td>${fx(econ.per_mu_cost, 0)} 元</td>` +
       `<td><b style="color:${econ.per_mu_net < 0 ? '#c0392b' : '#256b45'}">${fx(econ.per_mu_net, 0)} 元/亩</b></td>` +
       `<td><b>${money(econ.total_net)} 元</b></td></tr></table>`;
  h += `<div class="line" style="margin-top:9px">跌到这两个数就白干了：
    保本收购价 <b>${econ.break_even_price === null ? '-' : fx(econ.break_even_price, 2) + ' 元/公斤'}</b> ·
    保本亩产 <b>${econ.break_even_yield === null ? '-' : fx(econ.break_even_yield, 0) + ' 公斤'}</b></div>`;
  if(econ.updated) h += `<div class="hint">测算于 ${esc(econ.updated)}　·　数据可在经济效益测算页修改</div>`;
  h += `<div class="line" style="margin-top:8px"><a href="/econ-page?plot=${encodeURIComponent(plotId)}"
     style="color:#2F5597;font-weight:600;text-decoration:none">重新测算这块地 →</a></div>`;
  return h + `</div>`;
}

/* 施肥区块（当前阶段与历史阶段共用）
   只列「本阶段施用方式匹配得上」的肥料，并标出本阶段该用哪种方式；
   遇到成熟收获这类不该施肥的阶段，直接给提醒而不是列一堆肥料。 */
function fertBlock(f){
  if(!f) return '';
  let h = `<div class="sect"><h3>施肥要点：${esc(f.title)}</h3><div class="line">${esc(f.tip)}</div>`;
  if(f.stop || !(f.fertilizers || []).length){
    h += `<div class="warnbar" style="margin-top:9px">${esc(f.note || '本阶段不推荐施用肥料，以控水控肥、促进成熟为主。')}</div>`;
  } else {
    h += `<table><tr><th>推荐肥料</th><th>类型</th><th>养分含量</th><th>本阶段施用方式</th><th>特点</th></tr>` +
      f.fertilizers.map(x => `<tr>
        <td><b>${esc(x.name)}</b></td>
        <td>${esc(x.type || '-')}</td>
        <td>${esc(x.nutrient || '-')}</td>
        <td><span class="tag green">${esc(x.stage_method || '-')}</span>
            <span style="color:#94a3b8;font-size:11.5px" title="该肥料支持的全部施用方式">全部：${esc(x.method || '-')}</span></td>
        <td>${esc(x.note || '-')}</td></tr>`).join('') + `</table>`;
    if((f.methods || []).length){
      h += `<div class="hint">本阶段推荐施用方式：${f.methods.map(m => `<span class="tag green">${esc(m)}</span>`).join('')}` +
           `　·　表中只列出与这些方式匹配的肥料，换一个生育期会换成另一批。</div>`;
    }
  }
  if((f.nutrients || []).length){
    h += `<div class="line" style="margin-top:8px;font-size:12.5px;color:#64748b">该作物需要的养分：` +
      f.nutrients.map(n => esc(n.name)).join('、') + `</div>`;
  }
  if(f.soil_note){
    h += `<div class="warnbar" style="margin-top:9px"><b>结合土质调整：</b>${esc(f.soil_note)}</div>`;
  }
  return h + `</div>`;
}

/* 整份提醒 */
function renderReminder(d){
  if(!d || d.status === 'unknown') return `<div class="empty">${esc((d && d.message) || '暂无数据')}</div>`;
  return renderReminderHead(d) + renderReminderBody(d);
}

/* 头部：作物 / 播种日 / 今天 + 当前阶段说明 + 下一阶段预警
   （这一块跟随"当前阶段"，不随时间轴上选中的时期变化） */
function renderReminderHead(d){
  if(!d || d.status === 'unknown') return '';
  const stages = d.stages || [];
  const head = `<div class="line"><b>${esc(d.crop)}</b>　播种 ${esc(d.sow_date)}　今天 ${esc(d.today)}
    ${d.total_days ? '　全生育期 ' + d.total_days + ' 天' : ''}</div>`;

  if(d.status === 'before_sow'){
    return head + `<div class="okbar">${esc(d.message)}</div>`;
  }
  if(d.status === 'done'){
    return head + `<div class="warnbar">${esc(d.message)}</div>`;
  }

  const cur = d.stage;
  let html = head;
  html += `<div class="okbar" style="font-size:14px">${esc(d.message)}</div>`;
  html += `<div class="line" style="margin-top:8px;font-size:12px;color:#94a3b8">
    本阶段第 ${d.stage_day}/${cur.days} 天 · 全生育期进度 ${d.overall_percent}%</div>`;
  if(d.next_soon && d.next_stage){
    html += `<div class="warnbar"><b>提前预警：</b>还有 ${d.days_to_next} 天进入【${esc(d.next_stage.short)}】，
      建议现在就准备${(d.next_todo || []).slice(0, 3).map(p => esc(p.name)).join('、') || '下一阶段农事'}。
      ${(d.next_risks || []).length ? '该阶段需重点防：' + (d.next_risks || []).slice(0, 4).map(r => esc(r.name)).join('、') + '。' : ''}</div>`;
  }
  return html;
}

/* 正文：当前阶段的农事 / 施肥 / 风险（选中其它时期时，这一块会被换成那个时期的） */
function renderReminderBody(d){
  if(!d || d.status === 'unknown') return `<div class="empty">${esc((d && d.message) || '暂无数据')}</div>`;
  if(d.status === 'before_sow' || d.status === 'done'){
    return `<div class="hint" style="margin-top:0">该生育期尚未开始或已结束，没有当期农事与风险；可点上方的生育期方块查看各阶段的安排。</div>`;
  }
  const cur = d.stage;
  let html = '';

  html += `<div class="sect" style="margin-top:0;border-top:none;padding-top:0"><h3>当前阶段农事（${esc(cur.short)}）</h3>`;
  const todo = d.todo || [];
  html += todo.length
    ? `<table><tr><th>农事操作</th><th>适宜时期</th><th>作用</th></tr>` +
      todo.map(p => `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.timing || '-')}</td><td>${esc(p.effect || '-')}</td></tr>`).join('') + `</table>`
    : '<div class="empty">该阶段暂无具体农事条目</div>';
  html += `</div>`;

  html += fertBlock(d.fertilize);

  const risks = d.risks || [];
  html += `<div class="sect"><h3>当期病虫害与草害风险${risks.length ? '（' + risks.length + ' 项）' : ''}</h3>`;
  if(!risks.length){
    html += `<div class="okbar">当前阶段图谱内没有标记为高发的病虫害，保持田间巡查即可。</div>`;
  } else {
    html += `<table><tr><th>名称</th><th>类别</th><th>危害等级</th><th>危害部位</th><th>常发时期</th><th>防治措施</th></tr>` +
      risks.map(r => {
        const lv = r.level === '重' ? 'tag red' : (r.level === '中' ? 'tag amber' : 'tag gray');
        return `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.label)}</td>
          <td><span class="${lv}">${esc(r.level || '未分级')}</span></td>
          <td>${esc(r.part || '-')}</td><td>${esc(r.period || '-')}</td>
          <td>${(r.measures || []).map(m => `<span class="tag green">${esc(m)}</span>`).join('') || '-'}</td></tr>`;
      }).join('') + `</table>`;
    html += `<div class="hint">防治顺序遵循「农业防治 → 生物防治 → 物理防治 → 化学农药」；使用化学农药请严格执行安全间隔期，采收前禁用。</div>`;
  }
  html += `</div>`;
  return html;
}
