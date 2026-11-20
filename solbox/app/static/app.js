/* 작은 화면 보강. 모두 선택 사항이라 스크립트가 없어도 화면은 쓸 수 있다. */
(function () {
  "use strict";

  function hashId() {
    var h = location.hash.slice(1);
    try { h = decodeURIComponent(h); } catch (e) { /* 깨진 조각은 그대로 둔다 */ }
    return h;
  }

  // 좁은 화면 상단 메뉴: 현재 항목이 보이게 메뉴 안에서만 가로로 옮기고, 끝에 닿으면 끝 표시를 감춘다
  var topNav = document.querySelector(".topbar nav");
  if (topNav) {
    var syncNav = function () { topNav.classList.toggle("at-end", topNav.scrollLeft + topNav.clientWidth >= topNav.scrollWidth - 2); };
    var curItem = topNav.querySelector('[aria-current="page"]');
    if (curItem && topNav.scrollWidth > topNav.clientWidth) {
      topNav.scrollLeft += curItem.getBoundingClientRect().left - topNav.getBoundingClientRect().left - (topNav.clientWidth - curItem.offsetWidth) / 2;
    }
    topNav.addEventListener("scroll", syncNav);
    window.addEventListener("resize", syncNav);
    syncNav();
  }

  // 체크 상자 목록: 선택 수 표시와 걸러 보기
  document.querySelectorAll(".picker").forEach(function (box) {
    var count = box.querySelector("[data-count]");
    var items = box.querySelectorAll(".picker-list label");
    var filter = box.querySelector(".picker-filter");
    box.addEventListener("change", function () {
      var n = box.querySelectorAll(".picker-list input:checked").length;
      if (count) count.textContent = n ? " · " + n + "개 선택" : "";
    });
    if (!filter) return;
    filter.hidden = false;
    filter.addEventListener("keydown", function (e) {
      if (e.key === "Enter") e.preventDefault(); // 걸러 보기에서 Enter 로 폼이 저장되지 않게
    });
    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      items.forEach(function (l) {
        var name = l.querySelector(".pname");
        l.hidden = q !== "" && (name ? name.textContent : l.textContent).toLowerCase().indexOf(q) < 0;
      });
    });
  });

  // 제안 상세: 주소 조각의 구역을 펼친다
  var bar = document.querySelector(".stepbar ol");
  if (bar) {
    var sections = { target: 1, directions: 1, chat: 1, outline: 1, deck: 1 };
    var reveal = function (id) {
      if (!Object.prototype.hasOwnProperty.call(sections, id)) return;
      var el = document.getElementById(id);
      if (!el) return;
      for (var d = el; d; d = d.parentElement) { if (d.tagName === "DETAILS") d.open = true; }
      el.scrollIntoView();
    };
    window.addEventListener("hashchange", function () { reveal(hashId()); });
    bar.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        history.replaceState(null, "", a.getAttribute("href"));
        reveal(a.getAttribute("href").slice(1));
      });
    });
    var cur = bar.querySelector(".current");
    if (cur) bar.scrollLeft += cur.getBoundingClientRect().left - bar.getBoundingClientRect().left - (bar.clientWidth - cur.offsetWidth) / 2;
    reveal(hashId());
  }

  // 구성안: 확인이 남은 장만 보기
  document.querySelectorAll(".remaining-toggle").forEach(function (btn) {
    btn.hidden = false;
    var KEY = "solbox.outline.remaining";
    var apply = function (on) {
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? "전체 보기" : "남은 장만 보기";
      document.querySelectorAll(".slide-card").forEach(function (c) { c.hidden = on && !c.dataset.remaining; });
    };
    btn.addEventListener("click", function () {
      var on = btn.getAttribute("aria-pressed") !== "true";
      apply(on);
      try { sessionStorage.setItem(KEY, on ? "1" : "0"); } catch (e) { /* 기억은 선택 사항 */ }
    });
    try { if (sessionStorage.getItem(KEY) === "1") apply(true); } catch (e) { /* 저장소를 못 쓰면 전체 보기 */ }
  });

  // 방향 카드: 출처 번호를 누르면 접힌 부분을 펼치고 그 출처로 이동한다
  document.querySelectorAll(".dir-card sup a").forEach(function (a) {
    a.addEventListener("click", function () {
      var t = document.getElementById(a.getAttribute("href").slice(1));
      for (var d = t; d; d = d.parentElement) { if (d.tagName === "DETAILS") d.open = true; }
    });
  });

  // 구성안: 모두 펼치기/접기, 펼친 장 기억
  var getCards = function () { return Array.prototype.slice.call(document.querySelectorAll("details.slide-card")); };
  if (getCards().length) {
    var OPEN_KEY = "solbox.outline.open." + location.pathname;
    var saveOpen = function () {
      var ids = getCards().filter(function (c) { return c.open; }).map(function (c) { return c.id; });
      try { sessionStorage.setItem(OPEN_KEY, JSON.stringify(ids)); } catch (e) { /* 기억은 선택 사항 */ }
    };
    // 기억해 둔 장을 다시 펼친다. 장 번호가 바뀐 처리(이동·삭제·추가, ?renumber=1)는 기억을 버리고 서버가 연 장만 남긴다
    var params = new URLSearchParams(location.search);
    if (!params.has("renumber")) {
      try {
        var saved = JSON.parse(sessionStorage.getItem(OPEN_KEY) || "[]");
        getCards().forEach(function (c) { if (saved.indexOf(c.id) >= 0) c.open = true; });
      } catch (e) { /* 저장소를 못 쓰면 서버가 정한 상태 그대로 */ }
    }
    saveOpen();
    // 주소의 ?open=, ?renumber= 는 한 번 쓰고 지운다. 새로고침하면 기억해 둔 상태를 따른다
    if (params.has("open") || params.has("renumber")) {
      params.delete("open"); params.delete("renumber");
      var rest = params.toString();
      try { history.replaceState(null, "", location.pathname + (rest ? "?" + rest : "") + location.hash); } catch (e) { /* 그대로 둔다 */ }
    }
    document.addEventListener("toggle", function (e) { if (e.target.matches && e.target.matches("details.slide-card")) saveOpen(); }, true);
    document.querySelectorAll(".toggle-slides").forEach(function (box) {
      box.hidden = false;
      box.addEventListener("click", function (e) {
        var b = e.target.closest("button");
        if (!b) return;
        getCards().forEach(function (c) { c.open = b.dataset.open === "1"; });
        saveOpen();
      });
    });
    var revealSlide = function () {
      var c = document.getElementById(hashId());
      if (c && c.matches("details.slide-card")) c.open = true;
    };
    window.addEventListener("hashchange", revealSlide);
    revealSlide();

    // 확인함 표시는 화면을 다시 불러오지 않고 그 장만 바꾼다. 장의 항목을 다 확인하면 접고 다음 남은 장을 연다
    var swapChecked = function (form) {
      var card = form.closest("details.slide-card");
      return fetch(form.action, { method: "POST", credentials: "same-origin", body: new URLSearchParams(new FormData(form)) })
        .then(function (r) { if (!r.ok) throw new Error("status " + r.status); return r.text(); })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var fresh = doc.getElementById(card.id);
          card = document.getElementById(card.id); // 응답을 기다리는 사이 다른 확인이 카드를 바꿨을 수 있어 바꿀 때 다시 찾는다
          if (!fresh || !card) throw new Error("no card");
          var wasOpen = card.open;
          fresh.open = wasOpen;
          var pressed = document.querySelector(".remaining-toggle[aria-pressed=true]");
          fresh.hidden = !!pressed && !fresh.dataset.remaining;
          card.replaceWith(fresh);
          [".count-evidence", ".count-decide", ".count-done"].forEach(function (sel) {
            var from = doc.querySelector(sel), to = document.querySelector(sel);
            if (from && to) to.textContent = from.textContent;
          });
          var deck = doc.getElementById("deck"), curDeck = document.getElementById("deck");
          if (deck && curDeck) curDeck.innerHTML = deck.innerHTML;
          initSlideForms(fresh);
          if (!fresh.dataset.remaining && wasOpen) {
            fresh.open = false;
            var all = getCards(), at = all.indexOf(fresh);
            var next = all.slice(at + 1).concat(all.slice(0, at)).filter(function (c) { return c.dataset.remaining; })[0];
            if (next) { next.open = true; next.scrollIntoView({ block: "nearest" }); }
          }
          saveOpen();
        });
    };
    // 확인 요청은 하나씩 차례로 보낸다 (서버의 읽고-고치고-쓰기와 화면 바꾸기가 겹쳐 한 쪽 확인이 사라지지 않게)
    var checkQueue = Promise.resolve();
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!window.fetch || !window.DOMParser || !form.matches || !form.matches("form[data-ajax-check]")) return;
      e.preventDefault();
      var btn = form.querySelector("button");
      if (btn) btn.disabled = true;
      var run = function () {
        return swapChecked(form).catch(function () { form.submit(); }); // 안 되면 보통 제출로 되돌린다
      };
      checkQueue = checkQueue.then(run, run);
    });
  }

  // 구성안 편집 폼: 종류에 맞는 칸만 보인다
  function initSlideForms(root) {
    root.querySelectorAll(".slide-form").forEach(function (form) {
      var sel = form.querySelector(".kind-select");
      if (!sel) return;
      var sync = function () {
        form.querySelectorAll("[data-kinds]").forEach(function (el) { el.hidden = el.dataset.kinds.split(" ").indexOf(sel.value) < 0; });
      };
      sync();
      sel.addEventListener("change", sync);
    });
  }
  initSlideForms(document);

  // 리서치 상세: 모두 펼치기/접기, 분야 탭
  document.querySelectorAll(".toggle-all").forEach(function (box) {
    box.hidden = false;
    box.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      var target = document.getElementById(box.dataset.target);
      if (!b || !target) return;
      target.querySelectorAll("details.trend-card, details.plan-edit").forEach(function (d) { d.open = b.dataset.open === "1"; });
    });
  });
  // 올리기 확인 화면: 고치기를 닫을 때 요약 갱신, 제외 체크에 따라 저장 막대의 건수 갱신
  document.querySelectorAll(".plan-item").forEach(function (card) {
    var edit = card.querySelector("details.plan-edit");
    if (!edit) return;
    var key = card.id.replace(/^item-/, "");
    edit.addEventListener("toggle", function () {
      if (edit.open) return;
      var summary = edit.querySelector("summary"), head = card.querySelector(".plan-title");
      if (summary && head) summary.setAttribute("aria-label", head.textContent.trim() + " 고치기");
      card.querySelectorAll("[data-sum]").forEach(function (el) {
        if (el.dataset.sum === "line") { // 연락처 덧붙이기: 칸 값으로 덧붙일 줄을 다시 만든다
          var val = function (n) { var i = card.querySelector('[name="' + key + "__" + n + '"]'); return i ? i.value.replace(/\s+/g, " ").trim() : ""; };
          var parts = [[val("person"), val("role")].filter(Boolean).join(" "), val("phone"), val("email"), val("address"), val("note")].filter(Boolean);
          if (parts.length) el.textContent = parts.join(" · ") + (el.dataset.suffix ? " (" + el.dataset.suffix : "");
          else if (el.dataset.empty) el.textContent = el.dataset.empty;
          return;
        }
        var input = card.querySelector('[name="' + key + "__" + el.dataset.sum + '"]');
        if (!input) return;
        var text = input.tagName === "SELECT" ? (input.selectedOptions[0] || {}).textContent : input.value;
        if (text && text.trim()) el.textContent = text.trim();
        else if (el.dataset.empty) el.textContent = el.dataset.empty; // 칸을 비우면 처음 서버가 그린 자리 표시 글로
      });
    });
  });
  var planBar = document.querySelector(".plan-savebar");
  if (planBar) {
    var planForm = planBar.closest("form");
    var planCount = function () {
      var total = parseInt(planBar.dataset.total, 10) || 0;
      var skipped = planForm.querySelectorAll(".plan-skip input:checked").length;
      var saved = total - skipped;
      planBar.querySelector("[data-save-button]").textContent = "이대로 저장 (" + saved + "건)";
      planBar.querySelector("[data-save-count]").textContent = saved + "건 저장" + (skipped ? " · " + skipped + "건 제외" : "");
    };
    planForm.addEventListener("change", function (e) {
      if (e.target.closest && e.target.closest(".plan-skip")) planCount();
    });
    planCount();
  }
  var wrap = document.getElementById("area-tabs");
  if (wrap) {
    var tablist = wrap.querySelector(".tabs");
    var tabs = Array.prototype.slice.call(wrap.querySelectorAll(".tabs a"));
    var panels = Array.prototype.slice.call(wrap.querySelectorAll(".tabpanel"));
    var ids = panels.map(function (p) { return p.id; });
    tablist.setAttribute("role", "tablist");
    tabs.forEach(function (t, i) {
      t.setAttribute("role", "tab");
      t.setAttribute("aria-controls", ids[i]);
      panels[i].setAttribute("role", "tabpanel");
      panels[i].setAttribute("aria-labelledby", t.id);
    });
    var select = function (id, focus) {
      var at = ids.indexOf(id);
      if (at < 0) return;
      tabs.forEach(function (t, i) {
        t.setAttribute("aria-selected", i === at ? "true" : "false");
        t.tabIndex = i === at ? 0 : -1;
        panels[i].hidden = i !== at;
      });
      if (focus) tabs[at].focus();
    };
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function (e) {
        e.preventDefault();
        history.replaceState(null, "", "#" + ids[i]);
        select(ids[i]);
      });
      t.addEventListener("keydown", function (e) {
        var step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        e.preventDefault();
        var next = (i + step + ids.length) % ids.length;
        history.replaceState(null, "", "#" + ids[next]);
        select(ids[next], true);
      });
    });
    window.addEventListener("hashchange", function () { select(hashId()); });
    select(ids.indexOf(hashId()) >= 0 ? hashId() : ids[0]);
  }
  // 올리기: 끌어다 놓은 파일을 같은 파일 입력칸에 더하고, 고른 파일 목록·개수·합계를 보여 준다. 없어도 파일 선택 버튼으로 올릴 수 있다.
  function sizeText(bytes) {
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + "KB";
    return (bytes / (1024 * 1024)).toFixed(1) + "MB";
  }
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector('input[type="file"]');
    if (!input) return;
    var list = zone.querySelector(".dropzone-list");
    var summary = zone.querySelector(".dropzone-summary");
    var warn = zone.querySelector(".dropzone-warn");
    var maxFiles = parseInt(zone.getAttribute("data-max-files"), 10) || 0;
    var maxMb = parseFloat(zone.getAttribute("data-max-mb")) || 0;
    var show = function () {
      var files = Array.prototype.slice.call(input.files || []);
      var total = 0;
      var tooBig = [];
      if (list) list.textContent = "";
      files.forEach(function (f) {
        total += f.size;
        if (maxMb && f.size > maxMb * 1024 * 1024) tooBig.push(f.name);
        if (!list) return;
        var li = document.createElement("li");
        li.textContent = f.name + " (" + sizeText(f.size) + ")";
        list.appendChild(li);
      });
      if (summary) {
        summary.hidden = files.length === 0;
        summary.textContent = files.length ? files.length + "개 선택 · 합계 " + sizeText(total) : "";
      }
      var messages = [];
      if (maxFiles && files.length > maxFiles) messages.push("한 번에 최대 " + maxFiles + "개까지 올릴 수 있어요. " + files.length + "개를 골랐으니 " + (files.length - maxFiles) + "개를 빼 주세요.");
      if (tooBig.length) messages.push("파일 하나는 최대 " + maxMb + "MB까지예요. 너무 큰 파일: " + tooBig.join(", "));
      if (warn) {
        warn.hidden = messages.length === 0;
        warn.textContent = messages.join(" ");
      }
    };
    // 끌어다 놓은 파일은 이미 고른 파일에 더한다 (이름·크기·수정 시각이 같으면 한 번만)
    var add = function (dropped) {
      var merged;
      try {
        merged = new DataTransfer();
        var seen = {};
        Array.prototype.concat.call(Array.prototype.slice.call(input.files || []), Array.prototype.slice.call(dropped)).forEach(function (f) {
          var key = f.name + "|" + f.size + "|" + f.lastModified;
          if (seen[key]) return;
          seen[key] = true;
          merged.items.add(f);
        });
      } catch (err) {
        input.files = dropped; // DataTransfer 를 쓸 수 없는 브라우저: 놓은 파일로 바꾼다
        show();
        return;
      }
      input.files = merged.files;
      show();
    };
    input.addEventListener("change", show);
    zone.classList.add("enhanced"); // 파일 입력칸은 감추고 영역 전체(label)를 누르게 한다
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.add("over"); });
    });
    zone.addEventListener("dragleave", function (e) {
      if (!e.relatedTarget || !zone.contains(e.relatedTarget)) zone.classList.remove("over");
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      zone.classList.remove("over");
      if (e.dataTransfer && e.dataTransfer.files.length) add(e.dataTransfer.files);
    });
    show();
  });
})();
