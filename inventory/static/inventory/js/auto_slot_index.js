(function () {
    function getNextSlotIndex() {
        var inputs = document.querySelectorAll('input[name$="-slot_index"]');
        var used = [];
        inputs.forEach(function (inp) {
            var v = parseInt(inp.value, 10);
            if (!isNaN(v) && v >= 0) used.push(v);
        });
        used.sort(function (a, b) { return a - b; });
        var idx = 0;
        for (var i = 0; i < used.length; i++) {
            if (used[i] === idx) idx++;
            else if (used[i] > idx) break;
        }
        return idx;
    }

    function patchNewSlotInput(container) {
        if (!container) return;
        var inp = container.querySelector
            ? container.querySelector('input[name$="-slot_index"]')
            : null;
        if (inp && (inp.value === "" || parseInt(inp.value, 10) < 0)) {
            inp.value = getNextSlotIndex();
        }
    }

    document.addEventListener("formset:added", function (e) {
        patchNewSlotInput(e.target);
    });
    document.addEventListener("django:formset:added", function (e) {
        patchNewSlotInput(e.target);
    });

    var observer = new MutationObserver(function (mutations) {
        mutations.forEach(function (m) {
            m.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) patchNewSlotInput(node);
            });
        });
    });

    function startObserver() {
        var inlineGroup = document.querySelector(".inline-group");
        if (inlineGroup) {
            observer.observe(inlineGroup, { childList: true, subtree: true });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startObserver);
    } else {
        startObserver();
    }
})();
