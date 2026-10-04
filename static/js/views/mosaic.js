/* 视图：照片马赛克——目标图切网格，每格填入颜色最接近的素材小图。 */
window.Views = window.Views || {};
window.Views.mosaic = (function () {
  const C = window.Common;
  let targetId = null;
  let materialIds = new Set();
  let images = [];
  let lastResult = null;

  return {
    mount(el) {
      el.innerHTML = `
        <div class="split">
          <div class="col">
            <div class="panel">
              <div class="panel-title">① 目标底样<span class="dim">远看呈现这张图的轮廓</span></div>
              <div id="mc-target-gallery" style="max-height:240px;overflow:auto"></div>
            </div>
            <div class="panel">
              <div class="panel-title">② 素材小图<span class="dim">可多选，已选 <b id="mc-count">0</b> 张</span></div>
              <div class="toolbar">
                <button class="btn btn-sm" id="mc-select-all">全选</button>
                <button class="btn btn-sm" id="mc-select-clear">清空</button>
              </div>
              <div id="mc-material-gallery" style="max-height:300px;overflow:auto"></div>
            </div>
            <div class="panel">
              <div class="panel-title">③ 参数</div>
              <div class="field"><label>网格密度（列数）<span class="hint">格越多越像底样</span></label>
                <div class="range-row"><input type="range" id="mc-cols" min="8" max="120" step="1" value="48"><span class="range-val">48</span></div>
              </div>
              <div class="field"><label>每格边长（像素）<span class="hint">越大越清晰、文件越大</span></label>
                <div class="range-row"><input type="range" id="mc-cell" min="16" max="80" step="2" value="40"><span class="range-val">40</span></div>
              </div>
              <div class="field"><label>素材填充方式<span class="hint">横竖不一的素材也能填满每格</span></label>
                <select id="mc-fit">
                  <option value="cover">裁剪填满（等比缩放+居中裁剪，推荐）</option>
                  <option value="stretch">拉伸填满（不变白边，但会变形）</option>
                  <option value="contain">完整居中（多余区域用模糊同款图垫底）</option>
                </select>
              </div>
              <div class="field"><label>实际使用素材数<span class="hint" id="mc-count-hint">0 = 使用全部所选素材</span></label>
                <input type="number" id="mc-mcount" value="0" min="0" step="1">
              </div>
              <div class="field"><label>素材重复<span class="hint">格数多于素材时如何复用</span></label>
                <select id="mc-repeat">
                  <option value="avoid">均匀分散（避免相邻重复，推荐）</option>
                  <option value="allow">允许重复（只按颜色最近，可能扎堆）</option>
                </select>
              </div>
              <div class="field"><label>比色空间</label>
                <select id="mc-color">
                  <option value="lab">Lab（人眼感知，推荐）</option>
                  <option value="rgb">加权 RGB（更快）</option>
                </select>
              </div>
              <div class="field"><label>底样轮廓增强<span class="hint">把目标色调叠加到马赛克上</span></label>
                <div class="range-row"><input type="range" id="mc-tint" min="0" max="60" step="1" value="15"><span class="range-val">15%</span></div>
              </div>
              <button class="btn btn-primary" id="mc-run" style="width:100%">🧩 生成马赛克大图</button>
            </div>
          </div>
          <div class="col">
            <div class="panel">
              <div class="panel-title">马赛克结果<span class="dim" id="mc-meta"></span></div>
              <div class="stage" id="mc-result"><span class="dim">选择目标图与素材后点击生成</span></div>
              <div class="toolbar" id="mc-actions" style="margin-top:12px;display:none">
                <a class="btn btn-primary" id="mc-download" download="mosaic.png">⬇ 下载 PNG 大图</a>
                <span class="badge" id="mc-stat"></span>
              </div>
            </div>
          </div>
        </div>`;

      C.fetchImages().then((list) => {
        images = list;
        renderGalleries(el);
        document.getElementById("mc-count-hint").textContent =
          list.length ? `图库共 ${list.length} 张，0 = 使用全部所选素材` : "0 = 使用全部所选素材";
      });

      // 单选目标图
      el.querySelector("#mc-target-gallery").addEventListener("click", (e) => {
        const card = e.target.closest(".card");
        if (!card) return;
        targetId = card.dataset.id;
        el.querySelectorAll("#mc-target-gallery .card").forEach((c) => c.classList.toggle("selected", c === card));
      });

      // 多选素材
      el.querySelector("#mc-material-gallery").addEventListener("click", (e) => {
        const card = e.target.closest(".card");
        if (!card) return;
        const id = card.dataset.id;
        if (materialIds.has(id)) { materialIds.delete(id); card.classList.remove("selected"); }
        else { materialIds.add(id); card.classList.add("selected"); }
        updateCount(el);
      });

      el.querySelector("#mc-select-all").onclick = () => {
        materialIds = new Set(images.map((im) => im.id));
        syncSelection(el);
      };
      el.querySelector("#mc-select-clear").onclick = () => {
        materialIds.clear();
        syncSelection(el);
      };

      ["mc-cols", "mc-cell", "mc-tint"].forEach((rid) => {
        const inp = el.querySelector("#" + rid);
        inp.addEventListener("input", () => {
          const v = inp.value;
          inp.nextElementSibling.textContent = rid === "mc-tint" ? v + "%" : v;
        });
      });

      el.querySelector("#mc-run").onclick = () => run(el);
    },

    refresh() {
      const el = document.querySelector('.view[data-view="mosaic"]');
      if (!el || !this.mounted) return;
      C.refreshImages().then((list) => {
        // 丢弃已被删除的选择
        images = list;
        const valid = new Set(list.map((im) => im.id));
        targetId = valid.has(targetId) ? targetId : null;
        materialIds = new Set([...materialIds].filter((id) => valid.has(id)));
        renderGalleries(el);
      });
    },
  };

  function galleryItem(im, selected) {
    return `
      <div class="card${selected ? " selected" : ""}" data-id="${C.esc(im.id)}">
        <img class="thumb" src="${C.esc(im.thumbnail_url)}" loading="lazy" draggable="false">
        <span class="card-badge">${im.width}×${im.height}</span>
        <div class="card-meta">
          <div class="card-name" title="${C.esc(im.filename)}">${C.esc(im.filename)}</div>
        </div>
        <div class="mc-tick">✓</div>
      </div>`;
  }

  function renderGalleries(el) {
    const empty = `<div class="empty"><span class="big">🖼️</span>图库为空<br>请先到「图像管理」上传图片</div>`;
    el.querySelector("#mc-target-gallery").innerHTML =
      images.length ? `<div class="grid">${images.map((im) => galleryItem(im, im.id === targetId)).join("")}</div>` : empty;
    el.querySelector("#mc-material-gallery").innerHTML =
      images.length ? `<div class="grid">${images.map((im) => galleryItem(im, materialIds.has(im.id))).join("")}</div>` : empty;
    updateCount(el);
  }

  function syncSelection(el) {
    el.querySelectorAll("#mc-material-gallery .card").forEach((c) =>
      c.classList.toggle("selected", materialIds.has(c.dataset.id)));
    updateCount(el);
  }

  function updateCount(el) {
    el.querySelector("#mc-count").textContent = materialIds.size;
  }

  async function run(el) {
    if (!targetId) { C.toast("请先选择一张目标底样图", "error"); return; }
    if (!materialIds.size) { C.toast("请至少选择一张素材小图", "error"); return; }
    if (materialIds.has(targetId) && materialIds.size === 1) {
      C.toast("素材只有目标图本身，建议多选几张", "error");
      return;
    }
    const mcount = Number(el.querySelector("#mc-mcount").value) || 0;
    if (mcount < 0) { C.toast("素材数不能为负", "error"); return; }
    if (mcount > materialIds.size) { C.toast("素材数不能超过已选数量", "error"); return; }

    const body = {
      image_id: targetId,
      material_ids: [...materialIds],
      cols: Number(el.querySelector("#mc-cols").value),
      cell_size: Number(el.querySelector("#mc-cell").value),
      fit: el.querySelector("#mc-fit").value,
      material_count: mcount,
      repeat: el.querySelector("#mc-repeat").value,
      color_space: el.querySelector("#mc-color").value,
      tint: Number(el.querySelector("#mc-tint").value),
    };
    const stage = el.querySelector("#mc-result");
    stage.innerHTML = `<div class="loading">拼贴中…正在为 ${body.cols} 列网格逐格挑选素材</div>`;
    try {
      const r = await Api.post("/api/mosaic", body);
      lastResult = r;
      const url = `${r.file_url}?t=${Date.now()}`;
      stage.innerHTML = `<img src="${url}">`;
      el.querySelector("#mc-actions").style.display = "flex";
      el.querySelector("#mc-download").href = url;
      el.querySelector("#mc-meta").textContent =
        `${r.cols}×${r.rows} 格 · 每格 ${r.cell_size}px · 成品 ${r.width}×${r.height}`;
      el.querySelector("#mc-stat").textContent =
        `${r.cache_hit ? "缓存命中 · " : ""}入图素材 ${r.materials_used}` +
        (r.materials_pool > r.materials_used ? `/${r.materials_pool}` : `/${r.materials_total}`) +
        ` · 填充 ${r.fit}` +
        (r.skipped_materials ? ` · ${r.skipped_materials} 张无法读取已跳过` : "");
      if (r.skipped_materials) C.toast(`${r.skipped_materials} 张素材无法读取，已跳过`, "error");
    } catch (e) {
      stage.innerHTML = `<div class="empty"><span class="big">⚠️</span>${C.esc(e.message)}</div>`;
    }
  }
})();
