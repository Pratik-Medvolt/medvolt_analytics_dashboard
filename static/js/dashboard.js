document.addEventListener(
    "DOMContentLoaded",
    function () {
        const body = document.body;
        const sidebar = document.querySelector("[data-sidebar]");
        const toggleButton = document.querySelector("[data-sidebar-toggle]");
        const closeButton = document.querySelector("[data-sidebar-close]");
        const overlay = document.querySelector("[data-sidebar-overlay]");

        function openSidebar() {
            body.classList.add("sidebar-open");
            if (toggleButton) toggleButton.setAttribute("aria-expanded", "true");
        }

        function closeSidebar() {
            body.classList.remove("sidebar-open");
            if (toggleButton) toggleButton.setAttribute("aria-expanded", "false");
        }

        if (toggleButton) {
            toggleButton.addEventListener("click", openSidebar);
        }

        if (closeButton) {
            closeButton.addEventListener("click", closeSidebar);
        }

        if (overlay) {
            overlay.addEventListener("click", closeSidebar);
        }

        if (sidebar) {
            sidebar.querySelectorAll(".sidebar-link").forEach(function (link) {
                link.addEventListener("click", closeSidebar);
            });
        }

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") closeSidebar();
        });

        window.addEventListener("resize", function () {
            if (window.innerWidth > 760) closeSidebar();
        });
    }
);


// Shared chart palette — validated for CVD-safe categorical separation
// (see the project's dataviz color-formula: 3 slots, all-pairs checked).
window.MEDVOLT_CHART_COLORS = {
    green: "#1baf7a",
    blue: "#2a78d6",
    orange: "#eb6834",
    greenSoft: "rgba(27, 175, 122, 0.14)",
    blueSoft: "rgba(42, 120, 214, 0.12)",
    orangeSoft: "rgba(235, 104, 52, 0.12)",
    grid: "rgba(23, 36, 31, 0.06)",
    ink: "#6c7b75",
};

if (window.Chart) {
    Chart.defaults.font.family =
        '"Inter", -apple-system, sans-serif';
    Chart.defaults.color = window.MEDVOLT_CHART_COLORS.ink;
    Chart.defaults.borderColor = window.MEDVOLT_CHART_COLORS.grid;
}