(function ($) {
    function initSelect2(selector) {
        var $el = $(selector);
        if ($el.hasClass("select2-hidden-accessible")) return;

        $el.select2({
            placeholder: "-- 请选择食材 --",
            allowClear: true,
            width: "250px",
        });

        var prefix = selector.name.replace(/-item_selector$/, "");
        var itemIdInput = document.querySelector('input[name="' + prefix + '-item_id"]');
        var itemNameInput = document.querySelector('input[name="' + prefix + '-item_name"]');

        if (itemIdInput && itemIdInput.value) {
            $el.val(itemIdInput.value).trigger("change.select2");
        }

        $el.on("change", function () {
            if (!itemIdInput) return;
            var val = $el.val();
            if (!val) {
                itemIdInput.value = "";
                if (itemNameInput) itemNameInput.value = "";
                return;
            }
            itemIdInput.value = val;
            if (itemNameInput) {
                var text = $el.find("option:selected").text();
                var dashIndex = text.indexOf(" - ");
                itemNameInput.value = dashIndex !== -1 ? text.substring(dashIndex + 3) : text;
            }
        });
    }

    $(document).ready(function () {
        $('select[name$="-item_selector"]').each(function () {
            initSelect2(this);
        });

        $(document).on("formset:added", function (event, $row) {
            $row.find('select[name$="-item_selector"]').each(function () {
                initSelect2(this);
            });
        });
    });
})(django.jQuery);
