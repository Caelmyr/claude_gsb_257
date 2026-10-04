/* 视图：照片马赛克。目标图切块 + 图库素材逐格填色。 */
window.Views = window.Views || {};
window.Views.mosaic = (function () {
  const C = window.Common;

  // 与后端 config.MOSAIC_* 对齐；/api/config 返回后以后端为准
  const LIMITS = { cols_min: 10, cols_max: 120, max_materials: 400, max_output: 3000 };

  let el = null;
  let images = [];
  let targetId = null;
  let materials = new Set();
  let cfg = null;
  let busy = false;

  function materialCardHTML(im) {
    const on = materials.has(im.id);
    return `
      <div class="card mcard ${on ? "selected" : ""}" data-id="${C.esc(im.id)}">
        <img class="thumb" src="${esc(im.thumbnail_url)}" loading="lazy" draggable="false">
        <span class="card-badge">${im.width}×${im.height}</span>
        ${on ? '<span class="m-pick">✓</span>' : ""}
        <div class="card-meta">
          <div class="card-name" title="${C.esc(im.filename)}">${C.esc(im.filename)}</div>
          <div class="card-dim">${C.fmtBytes(im.size_bytes)}</div>
        </div>
      </div>`;
  }

  function esc(s) { return C.esc(s); }

  function renderMaterials() {
    const box = el.querySelector("#mo-materials");
    const count = el.querySelector("#mo-mat-count");
    count.textContent = `已选 ${materials.size} 张`;
    box.querySelectorAll(".mcard").forEach((card) => {
      const on = materials.has(card.dataset.id);
      card.classList.toggle("selected", on);
      let badge = card.querySelector(".m-pick");
      if (on && !badge) {
        badge = C.h('<span class="m-pick">✓</span>');
        card.appendChild(badge);
      } else if (!on && badge) {
        badge.remove();
      }
    });
  }

  function num(id) { return Number(el.querySelector(id).value); }

  function updateEstimate() {
    if (!images.length || !targetId) return;
    const rec = images.find((im) => im.id === targetId);
    if (!rec) return;
    const cols = num("#mo-cols");
    let tile = num("#mo-tile");
    const rows = Math.max(1, Math.round(cols * rec.height / rec.width));
    const maxDim = Math.max(cols, rows);
    const clamped = tile * maxDim > LIMITS.max_output;
    if (clamped) tile = Math.floor(LIMITS.max_output / maxDim);
    const w = cols * tile, h = rows * tile;
    el.querySelector("#mo-est").innerHTML =
      `网格 <b>${cols} × ${rows}</b> = ${cols * rows} 格 · 成品约 <b>${w} × ${h}</b>px` +
      (clamped ? `<span class="hint">（单格 ${num("#mo-tile")}px 会超出上限，自动收紧为 ${tile}px）</span>` : "");
  }

  async function loadImages() {
    images = await C.refreshImages();
    // 目标图库
    el.querySelector("#mo-targets").innerHTML = C.galleryHTML(images);
    C.bindGallery(el.querySelector("#mo-targets"), images, (id) => { targetId = id; updateEstimate(); });
    // 素材多选
    const box = el.querySelector("#mo-materials");
    box.innerHTML = `<div class="grid mgrid">${images.map(materialCardHTML).join("")}</div>`;
    box.onclick = (e) => {
      const card = e.target.closest(".mcard");
      if (!card) return;
      const id = card.dataset.id;
      if (materials.has(id)) materials.delete(id);
      else if (materials.size < LIMITS.max_materials) materials.add(id);
      else { C.toast(`素材最多 ${LIMITS.max_materials} 张`, "error"); return; }
      renderMaterials();
    };
    // 清理已失效选择
    const ids = new Set(images.map((im) => im.id));
    materials = new Set([...materials].filter((id) => ids.has(id)));
    renderMaterials();
    updateEstimate();
  }

  function bindRange(id) {
    el.querySelector(id).addEventListener("input", (e) => {
      e.target.nextElementSibling.textContent = e.target.value;
      updateEstimate();
    });
  }

  function setBusy(on) {
    busy = on;
    const btn = el.querySelector("#mo-run");
    btn.disabled = on;
    btn.textContent = on ? "生成中…" : "生成马赛克大图";
  }

  async function run() {
    if (busy) return;
    if (!targetId) { C.toast("请先选择一张目标图作为底样", "error"); return; }
    if (!materials.size) { C.toast("请至少勾选 1 张素材小图", "error"); return; }
    const body = {
      image_id: targetId,
      material_ids: [...materials],
      cols: num("#mo-cols"),
      tile_size: num("#mo-tile"),
      fit: el.querySelector("#mo-fit").value,
      contain_fill: el.querySelector("#mo-fill").value,
      max_materials: num("#mo-maxmat"),
      repeat_gap: num("#mo-gap"),
      blend: num("#mo-blend"),
    };
    setBusy(true);
    el.querySelector("#mo-result").innerHTML = `<div class="loading">正在切格、为 ${body.material_ids.length} 张素材匹配颜色…</div>`;
    try {
      const r = await Api.post("/api/mosaic", body);
      const warn = r.missing_materials
        ? `<span class="hint">（${r.missing_materials} 张素材已失效被跳过）</span>` : "";
      const clamp = r.tile_size_clamped
        ? `<span class="hint">（单格自动收紧为 ${r.tile_size}px 以保证成品不过大）</span>` : "";
      el.querySelector("#mo-result").innerHTML = `
        <img src="/api/results/${r.result_id}/file?t=${Date.now()}">
        <div class="caption">
          网格 ${r.cols}×${r.rows}（${r.cells} 格）· 单格 ${r.tile_size}px · 成品 ${r.width}×${r.height}px
          · 素材 ${r.materials_used}/${r.materials_pool} 张 · 平均色差 ΔE ${r.mean_color_error} ${warn} ${clamp}
        </div>
        <div class="toolbar" style="margin-top:10px">
          <a class="btn btn-primary btn-sm" href="/api/results/${r.result_id}/file" download="mosaic-${r.cols}x${r.rows}.png">下载 PNG</a>
          ${r.cache_hit ? '<span class="hint">命中缓存（参数与素材组合完全相同）</span>' : ""}
        </div>`;
    } catch (e) {
      el.querySelector("#mo-result").innerHTML = `<div class="empty"><span class="big">⚠️</span>${C.esc(e.message)}</div>`;
    } finally {
      setBusy(false);
    }
  }

  return {
    mount(root) {
      el = root;
      el.innerHTML = `
        <div class="panel">
          <div class="panel-title">① 目标图（底样）<span class="dim">马赛克远看呈现它的轮廓</span></div>
          <div id="mo-targets" style="max-height:260px;overflow:auto"></div>
        </div>
        <div class="panel">
          <div class="panel-title">② 素材小图（可多选）<span class="dim" id="mo-mat-count">已选 0 张</span></div>
          <div class="toolbar">
            <button class="btn btn-sm" id="mo-select-all">全选</button>
            <button class="btn btn-sm" id="mo-clear">清空</button>
            <span class="hint">尺寸/方向参差不齐没关系，系统会按下方方式统一缩放裁剪，保证每格填满</span>
          </div>
          <div id="mo-materials" style="max-height:340px;overflow:auto"></div>
        </div>
        <div class="split">
          <div class="col">
            <div class="panel">
              <div class="panel-title">③ 参数</div>
              <div class="field"><label>网格密度（列数）<span class="hint">行数按目标图比例自动计算</span></label>
                <div class="range-row"><input type="range" id="mo-cols" min="10" max="120" step="2" value="40"><span class="range-val">40</span></div>
              </div>
              <div class="field"><label>每格像素<span class="hint">越大近看越清晰、成品越大</span></label>
                <div class="range-row"><input type="range" id="mo-tile" min="16" max="96" step="4" value="40"><span class="range-val">40</span></div>
              </div>
              <div class="field"><label>素材数量上限<span class="hint">0 = 全部使用；超出时自动保留颜色覆盖最广的一组</span></label>
                <select id="mo-maxmat">
                  <option value="0">全部素材</option>
                  <option value="16">16 张</option>
                  <option value="32" selected>32 张</option>
                  <option value="64">64 张</option>
                  <option value="128">128 张</option>
                  <option value="200">200 张</option>
                  <option value="400">400 张</option>
                </select>
              </div>
              <div class="field"><label>每格缩放裁剪方式</label>
                <select id="mo-fit">
                  <option value="cover" selected">缩放裁剪（cover）：填满整格，裁掉超出部分，不变形</option>
                  <option value="contain">完整显示（contain）：整张可见，空白用底色补</option>
                  <option value="stretch">拉伸（stretch）：直接铺满，可能变形</option>
                </select>
              </div>
              <div class="field" id="mo-fill-field"><label>完整显示时的空白填充</label>
                <select id="mo-fill">
                  <option value="white" selected">白边</option>
                  <option value="black">黑边</option>
                  <option value="blur">模糊延伸</option>
                </select>
              </div>
              <div class="field"><label>重复间距<span class="hint">禁止同一张素材相邻出现；0 = 关闭</span></label>
                <select id="mo-gap">
                  <option value="0">关闭</option>
                  <option value="1" selected>1 格（八邻域不重复）</option>
                  <option value="2">2 格</option>
                  <option value="3">3 格</option>
                </select>
              </div>
              <div class="field"><label>颜色贴合度<span class="hint">把成品向目标颜色收敛；越高远看越像，近看仍为照片</span></label>
                <div class="range-row"><input type="range" id="mo-blend" min="0" max="100" step="5" value="20"><span class="range-val">20</span></div>
              </div>
              <div class="caption" id="mo-est" style="margin-bottom:10px"></div>
              <button class="btn btn-primary" id="mo-run">生成马赛克大图</button>
            </div>
          </div>
          <div class="col">
            <div class="panel">
              <div class="panel-title">马赛克结果</div>
              <div class="stage" id="mo-result"><span class="dim">选择目标图与素材后生成</span></div>
            </div>
          </div>
        </div>`;

      Api.get("/api/config").then((d) => {
        if (d && d.mosaic) {
          cfg = d.mosaic;
          LIMITS.cols_min = cfg.cols_min;
          LIMITS.cols_max = cfg.cols_max;
          LIMITS.max_materials = cfg.max_materials;
          LIMITS.max_output = cfg.max_output;
          el.querySelector("#mo-cols").min = cfg.cols_min;
          el.querySelector("#mo-cols").max = cfg.cols_max;
        }
        updateEstimate();
      }).catch(() => {});

      loadImages();

      bindRange("#mo-cols");
      bindRange("#mo-tile");
      bindRange("#mo-blend");

      el.querySelector("#mo-fit").addEventListener("change", (e) => {
        el.querySelector("#mo-fill-field").style.display = e.target.value === "contain" ? "" : "none";
      });
      el.querySelector("#mo-select-all").onclick = () => {
        materials = new Set(images.slice(0, LIMITS.max_materials).map((im) => im.id));
        renderMaterials();
      };
      el.querySelector("#mo-clear").onclick = () => { materials.clear(); renderMaterials(); };
      el.querySelector("#mo-maxmat").addEventListener("change", updateEstimate);
      el.querySelector("#mo-run").onclick = run;
    },

    refresh() {
      // 从其他页（如上传了新图）切回来时刷新图库
      if (el) loadImages();
    },
  };
})();
