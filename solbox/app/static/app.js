/* 솔루션 박스 공용 화면 동작. 스크립트가 없어도 화면은 그대로 쓸 수 있다. */
(function () {
  "use strict";

  // 파일 선택: 고른 파일 이름을 한국어로 보여 준다.
  function initFilePick(root) {
    root.querySelectorAll(".file-pick input[type=file]").forEach(function (input) {
      var out = input.closest(".file-pick").querySelector(".file-names");
      if (!out || input.dataset.bound) return;
      input.dataset.bound = "1";
      input.addEventListener("change", function () {
        var names = Array.prototype.map.call(input.files, function (f) { return f.name; });
        out.textContent = names.length === 0 ? "선택한 파일 없음"
          : names.length === 1 ? names[0] : names[0] + " 외 " + (names.length - 1) + "개";
      });
    });
  }

  // 표 정렬: <table class="sortable"> 의 th[data-sort] 를 누르면 그 열로 정렬한다.
  function cellValue(row, index, type) {
    var cell = row.children[index];
    var text = cell ? (cell.dataset.value || cell.textContent).trim() : "";
    if (type === "num") { var n = parseFloat(text.replace(/[^0-9.\-]/g, "")); return isNaN(n) ? -Infinity : n; }
    return text;
  }
  function initSortable(root) {
    root.querySelectorAll("table.sortable").forEach(function (table) {
      var heads = table.querySelectorAll("th[data-sort]");
      heads.forEach(function (th) {
        th.tabIndex = 0;
        function sort() {
          var index = Array.prototype.indexOf.call(th.parentNode.children, th);
          var asc = th.getAttribute("aria-sort") !== "ascending";
          heads.forEach(function (h) { h.removeAttribute("aria-sort"); });
          th.setAttribute("aria-sort", asc ? "ascending" : "descending");
          var body = table.tBodies[0];
          var rows = Array.prototype.slice.call(body.rows);
          var type = th.dataset.sort;
          rows.sort(function (a, b) {
            var x = cellValue(a, index, type), y = cellValue(b, index, type);
            var r = type === "num" ? x - y : String(x).localeCompare(String(y), "ko");
            return asc ? r : -r;
          });
          rows.forEach(function (r) { body.appendChild(r); });
        }
        th.addEventListener("click", sort);
        th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
      });
    });
  }

  // 연결 항목 체크 목록: 이름으로 거르기, 고른 개수 표시.
  function initPicker(root) {
    root.querySelectorAll(".picker").forEach(function (picker) {
      if (picker.dataset.bound) return;
      picker.dataset.bound = "1";
      var filter = picker.querySelector(".picker-filter");
      var count = picker.querySelector(".picker-count");
      var items = picker.querySelectorAll(".picker-list label");
      function update() {
        if (count) count.textContent = picker.querySelectorAll(".picker-list input:checked").length + "개 선택";
      }
      if (filter) {
        filter.addEventListener("input", function () {
          var q = filter.value.trim().toLowerCase();
          items.forEach(function (l) { l.hidden = q && l.textContent.toLowerCase().indexOf(q) < 0; });
        });
        filter.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
      }
      picker.addEventListener("change", update);
      update();
    });
  }

  // 관리 메뉴: 바깥을 누르거나 Esc 를 누르면 닫는다.
  function initMenu() {
    // 좁은 화면: 가로로 밀리는 메뉴에서 현재 메뉴가 보이게 한다.
    var nav = document.querySelector(".topbar nav"), cur = nav && nav.querySelector(":scope > [aria-current]");
    if (nav && cur && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft = cur.offsetLeft - (nav.clientWidth - cur.offsetWidth) / 2;
    }
    document.addEventListener("click", function (e) {
      document.querySelectorAll(".topbar details.menu[open]").forEach(function (d) {
        if (!d.contains(e.target)) d.removeAttribute("open");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") document.querySelectorAll(".topbar details.menu[open]").forEach(function (d) { d.removeAttribute("open"); });
    });
  }

  function init(root) { initFilePick(root); initSortable(root); initPicker(root); }
  document.addEventListener("DOMContentLoaded", function () { init(document); initMenu(); });
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
})();
