"use strict";
document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("affair-form");
    if (!form) return;
    const stages = document.getElementById("affair-stages");
    const addStage = document.getElementById("affair-add-stage");
    const counter = prefix => document.getElementById(`id_${prefix}-TOTAL_FORMS`);
    const full = prefix => Number(counter(prefix).value) >= Number(document.getElementById(`id_${prefix}-MAX_NUM_FORMS`).value);
    const field = (row, name) => row.querySelector(`[name$="-${name}"]`);
    const deleted = row => field(row, "DELETE").checked;
    const visibleStages = () => [...stages.querySelectorAll("[data-stage]")].filter(row => !deleted(row));
    const refresh = () => {
        visibleStages().forEach((row, i, all) => {
            field(row, "order").value = i + 1;
            row.querySelector("[data-stage-number]").textContent = `阶段 ${i + 1}`;
            row.querySelector('[data-move-stage="up"]').disabled = i === 0;
            row.querySelector('[data-move-stage="down"]').disabled = i === all.length - 1;
        });
        addStage.disabled = full("stages");
        form.querySelectorAll("[data-add-condition]").forEach(button => button.disabled = full("conditions"));
    };
    const toggleCondition = row => {
        const type = field(row, "type").value;
        row.querySelectorAll("[data-field]").forEach(box => {
            const name = box.dataset.field;
            const itemField = ["item_ids", "min_quality", "requires_freshness", "min_remaining_minutes"].includes(name);
            box.hidden = (itemField && type !== "item") || (name === "unit_minutes" && type !== "labor");
            box.querySelectorAll("input, select, textarea").forEach(input => input.disabled = box.hidden);
        });
    };
    const prepareCondition = row => {
        row.hidden = deleted(row);
        row.querySelector(".delete-field").hidden = true;
        row.querySelector("[data-remove-condition]").hidden = false;
        toggleCondition(row);
    };
    const prepareStage = row => {
        row.hidden = deleted(row);
        row.querySelector(".stage-name .delete-field").hidden = true;
        row.querySelectorAll(".stage-actions button, [data-add-condition]").forEach(button => button.hidden = false);
        row.querySelectorAll("[data-condition]").forEach(prepareCondition);
    };
    const clone = (prefix, templateId, selector) => {
        if (full(prefix)) return null;
        const index = Number(counter(prefix).value);
        const fragment = document.createElement("template");
        fragment.innerHTML = document.getElementById(templateId).innerHTML.replace(/__prefix__/g, String(index));
        counter(prefix).value = index + 1;
        return fragment.content.querySelector(selector);
    };
    const appendCondition = stage => {
        const row = clone("conditions", "affair-condition-template", "[data-condition]");
        if (!row) return;
        field(row, "stage_key").value = field(stage, "key").value;
        stage.querySelector("[data-condition-list]").appendChild(row);
        prepareCondition(row);
        refresh();
    };
    stages.querySelectorAll("[data-stage]").forEach(prepareStage);
    form.querySelectorAll("[data-condition]").forEach(prepareCondition);
    addStage.hidden = false;
    addStage.addEventListener("click", () => {
        const row = clone("stages", "affair-stage-template", "[data-stage]");
        if (!row) return;
        field(row, "key").value = crypto.randomUUID();
        field(row, "title").value = "";
        stages.appendChild(row);
        prepareStage(row);
        appendCondition(row);
        refresh();
        field(row, "title").focus();
    });
    form.addEventListener("change", event => {
        const row = event.target.closest("[data-condition]");
        if (row) toggleCondition(row);
    });
    form.addEventListener("click", event => {
        const button = event.target.closest("button");
        if (!button) return;
        const stage = button.closest("[data-stage]");
        if (button.hasAttribute("data-add-condition")) appendCondition(stage);
        if (button.hasAttribute("data-remove-condition")) {
            const row = button.closest("[data-condition]");
            field(row, "DELETE").checked = true;
            row.hidden = true;
        }
        if (button.hasAttribute("data-remove-stage")) {
            field(stage, "DELETE").checked = true;
            stage.querySelectorAll("[data-condition]").forEach(row => field(row, "DELETE").checked = true);
            stage.hidden = true;
        }
        if (button.dataset.moveStage) {
            const all = visibleStages(), index = all.indexOf(stage);
            if (button.dataset.moveStage === "up" && index > 0) stages.insertBefore(stage, all[index - 1]);
            if (button.dataset.moveStage === "down" && index < all.length - 1) stages.insertBefore(all[index + 1], stage);
        }
        refresh();
    });
    refresh();
});
