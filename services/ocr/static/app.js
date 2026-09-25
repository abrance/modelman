/* OCR 页面逻辑。
 *
 * 无构建、无框架、无依赖：一个 IIFE、一个状态对象、一个整体重绘函数。
 * 设计依据与取舍见 docs/ocr-ui.md，改行为前先读那一份。
 *
 * 两条硬规则（写在设计文档的「安全」一节）：
 *   1. 识别文本一律用 textContent 写入，绝不 innerHTML —— 内容来自图片，等同不可信输入；
 *   2. 位置框坐标只用 CSSOM 设置（el.style.left = …），因为 CSP 的 style-src 'self'
 *      会拦掉 HTML 里的 style="…" 属性。
 */

(() => {
    "use strict";

    // ── 常量 ──────────────────────────────────────────────────────────────

    const MAX_FILES = 20; // 一次最多排队多少张，防误选整本相册
    const COMPRESS_MAX_SIDE = 2000; // 压缩重试时的长边上限（像素）
    const COMPRESS_QUALITY = 0.85;
    const HISTORY_MAX_ITEMS = 50;
    const HISTORY_MAX_CHARS = 256 * 1024;

    const LS_TOKEN = "ocr-ui.token";
    const LS_MODEL = "ocr-ui.model";
    const LS_HISTORY = "ocr-ui.history.v1";

    // ── DOM ───────────────────────────────────────────────────────────────

    const $ = (id) => document.getElementById(id);

    const dom = {
        dropzone: $("dropzone"),
        fileInput: $("file-input"),
        cameraInput: $("camera-input"),
        cameraBtn: $("camera-btn"),
        clearBtn: $("clear-btn"),
        modelSelect: $("model-select"),
        modelHint: $("model-hint"),
        tokenDetails: $("token-details"),
        tokenInput: $("token-input"),
        tokenHint: $("token-hint"),
        progress: $("progress"),
        banner: $("banner"),
        cards: $("cards"),
        emptyHint: $("empty-hint"),
        copyAll: $("copy-all"),
        exportTxt: $("export-txt"),
        cardTemplate: $("card-template"),
        historyList: $("history-list"),
        historyEmpty: $("history-empty"),
        historyTemplate: $("history-item-template"),
        exportHistory: $("export-history"),
        clearHistory: $("clear-history"),
    };

    // ── 状态 ──────────────────────────────────────────────────────────────
    //
    // 渲染是「由状态整体重绘」的纯函数，不做增量 DOM 修补：几百行规模下这样最不容易出错。

    const state = {
        cards: [], // {id, file, name, sizeText, previewUrl, status, model, result, error, kind, elapsedMs, retryable}
        busy: false,
        nextId: 1,
    };

    /** id -> 档位说明，来自 /models；切档位时更新提示用。 */
    const modelsById = new Map();

    // ── 小工具 ────────────────────────────────────────────────────────────

    function formatBytes(bytes) {
        if (!Number.isFinite(bytes)) return "";
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
        return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    function formatTime(ts) {
        const d = new Date(ts);
        const pad = (n) => String(n).padStart(2, "0");
        return (
            `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
            `${pad(d.getHours())}:${pad(d.getMinutes())}`
        );
    }

    /** 单张卡片的文本：按服务返回顺序用换行拼接。 */
    function cardText(card) {
        if (!card.result) return "";
        return card.result.results.map((line) => line.text).join("\n");
    }

    /** 成功结果的合并文本：各张之间空行分隔（见设计文档「文本怎么拼」）。 */
    function allText() {
        return state.cards
            .filter((card) => card.status === "ok")
            .map(cardText)
            .filter((text) => text.length > 0)
            .join("\n\n");
    }

    function downloadText(filename, text) {
        const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function showBanner(message, kind) {
        dom.banner.textContent = message;
        dom.banner.classList.toggle("ok", kind === "ok");
        dom.banner.hidden = !message;
    }

    // ── 访问 token ────────────────────────────────────────────────────────

    function currentToken() {
        return localStorage.getItem(LS_TOKEN) || "";
    }

    function saveToken(value) {
        const token = value.trim();
        if (token) localStorage.setItem(LS_TOKEN, token);
        else localStorage.removeItem(LS_TOKEN);
        dom.tokenInput.classList.remove("invalid");
    }

    function requireToken(message) {
        dom.tokenDetails.open = true;
        dom.tokenInput.classList.add("invalid");
        dom.tokenHint.textContent = message;
        dom.tokenInput.focus();
    }

    // ── API ───────────────────────────────────────────────────────────────

    class OcrError extends Error {
        constructor(kind, message) {
            super(message);
            this.kind = kind; // unauthorized / too_large / bad_request / overloaded / network / server
        }
    }

    function classify(status, payload) {
        const message = (payload && (payload.error || payload.detail)) || `HTTP ${status}`;
        if (status === 401) return new OcrError("unauthorized", message);
        if (status === 503) return new OcrError("overloaded", message);
        if (status === 400) {
            return new OcrError(
                String(message).includes("MAX_IMAGE_BYTES") ? "too_large" : "bad_request",
                message,
            );
        }
        return new OcrError("server", message);
    }

    /** 识别一张图。返回 {results, time_ms, model}。 */
    async function recognize(file, model) {
        const form = new FormData();
        form.append("image", file, file.name || "image.png");

        const params = new URLSearchParams({ model, backend: "cpu" });
        const headers = {};
        const token = currentToken();
        if (token) headers["X-Auth-Token"] = token;

        let response;
        try {
            // 绝对路径：入口必须落在域名根路径，见设计文档「链路」
            response = await fetch(`/ocr?${params}`, { method: "POST", body: form, headers });
        } catch (err) {
            throw new OcrError("network", `无法连接服务：${err.message}`);
        }

        let payload = null;
        try {
            payload = await response.json();
        } catch {
            /* 非 JSON 响应交给下面的状态码分支处理 */
        }

        if (!response.ok) throw classify(response.status, payload);
        if (!payload || payload.success === false) {
            throw new OcrError("server", (payload && payload.error) || "识别失败");
        }
        return payload;
    }

    /** 打开页面时读档位清单与默认档位，两个端点都免鉴权。 */
    async function loadModels() {
        let models = [];
        let fallback = "";
        try {
            const version = await (await fetch("/version")).json();
            fallback = version.default_model || "";
        } catch {
            /* 取不到就退化成第一项，不阻塞使用 */
        }
        try {
            models = await (await fetch("/models")).json();
        } catch {
            models = [];
        }

        dom.modelSelect.replaceChildren();
        if (!models.length) {
            const option = document.createElement("option");
            option.value = fallback || "v6small";
            option.textContent = option.value;
            dom.modelSelect.appendChild(option);
            dom.modelHint.textContent = "取不到档位清单，只能使用默认档位。";
            return;
        }

        for (const info of models) {
            const option = document.createElement("option");
            option.value = info.id;
            option.textContent = info.available === false ? `${info.id}（不可用）` : info.id;
            option.disabled = info.available === false;
            option.title = info.label || "";
            dom.modelSelect.appendChild(option);
            modelsById.set(info.id, info.label || "");
        }

        const saved = localStorage.getItem(LS_MODEL);
        const wanted = saved || fallback;
        if (wanted && models.some((info) => info.id === wanted && info.available !== false)) {
            dom.modelSelect.value = wanted;
        }
        const active = currentModel();
        dom.modelHint.textContent = modelsById.get(active) || "";
    }

    function currentModel() {
        return dom.modelSelect.value || "v6small";
    }

    // ── 结果卡片 ──────────────────────────────────────────────────────────

    function addFiles(fileList) {
        const picked = Array.from(fileList || []).filter((file) =>
            file.type.startsWith("image/"),
        );
        const images = picked.length;
        const accepted = picked.slice(0, MAX_FILES);
        const rejected = picked.length - accepted.length;

        const notes = [];
        if (images < (fileList ? fileList.length : 0)) notes.push("已忽略非图片文件");
        if (rejected > 0) notes.push(`一次最多 ${MAX_FILES} 张，已忽略 ${rejected} 张`);
        showBanner(notes.join("；"));

        for (const file of accepted) {
            state.cards.push({
                id: state.nextId++,
                file,
                name: file.name || "剪贴板图片",
                sizeText: formatBytes(file.size),
                previewUrl: URL.createObjectURL(file),
                status: "idle",
                model: currentModel(),
                result: null,
                error: "",
                kind: "",
                elapsedMs: 0,
                retryable: false,
                compressed: false,
            });
        }
        render();
        void runQueue();
    }

    /** 逐张串行：每张出结果立刻显示，单张失败不影响其余。 */
    async function runQueue() {
        if (state.busy) return;
        state.busy = true;
        try {
            for (;;) {
                const card = state.cards.find((item) => item.status === "idle");
                if (!card) break;
                card.status = "sending";
                render();
                try {
                    const started = performance.now();
                    const payload = await recognize(card.file, card.model);
                    card.result = payload;
                    card.elapsedMs = Math.round(payload.time_ms ?? performance.now() - started);
                    card.status = "ok";
                    pushHistory(card);
                } catch (err) {
                    card.status = "error";
                    card.error = err.message;
                    card.kind = err.kind;
                    card.retryable = err.kind !== "bad_request" && err.kind !== "too_large";
                    if (err.kind === "unauthorized") {
                        requireToken("token 无效或缺失，请填写后重试。");
                    }
                }
                render();
            }
        } finally {
            state.busy = false;
            render();
        }
    }

    function averageConfidence(card) {
        const lines = card.result ? card.result.results : [];
        if (!lines.length) return 0;
        return lines.reduce((sum, line) => sum + (line.confidence || 0), 0) / lines.length;
    }

    function render() {
        const pending = state.cards.filter(
            (card) => card.status === "idle" || card.status === "sending",
        ).length;
        const done = state.cards.filter(
            (card) => card.status === "ok" || card.status === "error",
        ).length;

        if (state.cards.length === 0) {
            dom.progress.hidden = true;
        } else {
            dom.progress.hidden = false;
            dom.progress.textContent = pending
                ? `处理中 ${Math.min(done + 1, state.cards.length)}/${state.cards.length}`
                : `已处理 ${done}/${state.cards.length}`;
        }

        dom.emptyHint.hidden = state.cards.length > 0;
        dom.cards.replaceChildren(...state.cards.map(renderCard));
        dom.copyAll.disabled = !allText();
        dom.exportTxt.disabled = !allText();
    }

    function renderCard(card) {
        const node = dom.cardTemplate.content.firstElementChild.cloneNode(true);
        const pick = (role) => node.querySelector(`[data-role="${role}"]`);

        pick("name").textContent = card.name;
        const meta = [`${card.sizeText}`, card.model];
        if (card.status === "ok") {
            meta.push(`${card.result.results.length} 行`);
            meta.push(`${card.elapsedMs} ms`);
            meta.push(`置信度 ${(averageConfidence(card) * 100).toFixed(1)}%`);
        } else if (card.status === "sending") {
            meta.push("识别中…");
        }
        pick("meta").textContent = meta.join(" · ");

        const image = pick("image");
        image.src = card.previewUrl;
        image.alt = card.name;

        const errorNode = pick("error");
        if (card.status === "error") {
            errorNode.textContent = card.error;
            errorNode.hidden = false;
        }
        const textNode = pick("text");
        const boxesNode = pick("boxes");
        if (card.status === "ok") {
            renderLines(card, textNode, boxesNode, image);
        } else {
            textNode.textContent = card.status === "sending" ? "识别中…" : "";
        }

        const copyButton = pick("copy");
        copyButton.disabled = card.status !== "ok";
        copyButton.addEventListener("click", () => copyText(cardText(card), copyButton));

        const retryButton = pick("retry");
        if (card.status === "error" && card.retryable) {
            retryButton.hidden = false;
            retryButton.addEventListener("click", () => {
                card.status = "idle";
                card.error = "";
                render();
                void runQueue();
            });
        }
        // 压缩只做一次：反复压同一张图既没用又费时间，第二次还超限就让人来处理
        if (card.status === "error" && card.kind === "too_large") {
            if (card.compressed) {
                errorNode.textContent =
                    `${card.error}\n压缩后仍然超过上限：换一张图片，` +
                    `或让运维调大 MAX_IMAGE_BYTES。`;
            } else {
                retryButton.hidden = false;
                retryButton.textContent = "压缩后重试";
                retryButton.addEventListener("click", () => void compressAndRetry(card));
            }
        }

        return node;
    }

    function renderLines(card, textNode, boxesNode, image) {
        const lines = card.result.results;
        if (!lines.length) {
            textNode.textContent = "未识别到文字。";
            return;
        }

        const rows = [];
        const boxes = [];

        lines.forEach((line, index) => {
            const row = document.createElement("div");
            row.className = "line";
            row.appendChild(document.createTextNode(line.text));
            const confidence = document.createElement("span");
            confidence.className = "line-conf";
            confidence.textContent = `${(line.confidence * 100).toFixed(0)}%`;
            row.appendChild(confidence);

            let box = null;
            if (line.bbox) {
                box = document.createElement("div");
                box.className = "box";
                box.title = line.text;
                boxes.push({ box, bbox: line.bbox });
            }

            const activate = (on) => {
                row.classList.toggle("active", on);
                if (box) box.classList.toggle("active", on);
            };
            row.addEventListener("click", () => activate(true));
            row.addEventListener("blur", () => activate(false));
            if (box) {
                box.addEventListener("click", () => {
                    activate(true);
                    row.scrollIntoView({ block: "nearest" });
                });
            }

            rows.push(row);
            if (box) boxesNode.appendChild(box);
        });

        textNode.replaceChildren(...rows);

        // bbox 是原图像素坐标，用百分比定位：缩放窗口不用重算
        const place = () => {
            const width = image.naturalWidth;
            const height = image.naturalHeight;
            if (!width || !height) return;
            for (const { box, bbox } of boxes) {
                box.style.left = `${(bbox.left / width) * 100}%`;
                box.style.top = `${(bbox.top / height) * 100}%`;
                box.style.width = `${(bbox.width / width) * 100}%`;
                box.style.height = `${(bbox.height / height) * 100}%`;
            }
        };
        if (image.complete) place();
        else image.addEventListener("load", place, { once: true });
    }

    /** 只在大图超限时用，会改变精度，所以不做成默认行为。 */
    async function compressAndRetry(card) {
        card.compressed = true;
        try {
            card.file = await compressImage(card.file);
            card.sizeText = `${formatBytes(card.file.size)}（已压缩）`;
            card.status = "idle";
            card.error = "";
            render();
            void runQueue();
        } catch (err) {
            card.error = `压缩失败：${err.message}`;
            render();
        }
    }

    /**
     * 缩到长边上限后重编码。PNG 与 JPEG 都试一次，取更小的那个：
     * 纯色截图上 PNG 反而可能更小且无损，照片上 JPEG 更小，不挑格式就会
     * 出现"压缩完比原图还大"。
     */
    async function compressImage(file) {
        const bitmap = await createImageBitmap(file);
        const longest = Math.max(bitmap.width, bitmap.height);
        const scale = longest > COMPRESS_MAX_SIDE ? COMPRESS_MAX_SIDE / longest : 1;
        const width = Math.max(1, Math.round(bitmap.width * scale));
        const height = Math.max(1, Math.round(bitmap.height * scale));

        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        canvas.getContext("2d").drawImage(bitmap, 0, 0, width, height);
        bitmap.close();

        const png = await encode(canvas, "image/png");
        const jpeg = await encode(canvas, "image/jpeg", COMPRESS_QUALITY);
        const blob = jpeg && (!png || jpeg.size < png.size) ? jpeg : png;
        if (!blob) throw new Error("canvas 编码失败");

        const base = (file.name || "image").replace(/\.[^.]+$/, "");
        const extension = blob.type === "image/jpeg" ? "jpg" : "png";
        return new File([blob], `${base}-compressed.${extension}`, { type: blob.type });
    }

    function encode(canvas, type, quality) {
        return new Promise((resolve) => {
            canvas.toBlob((blob) => resolve(blob), type, quality);
        });
    }

    // ── 复制（三级降级） ──────────────────────────────────────────────────

    async function copyText(text, button) {
        if (!text) return;
        const original = button ? button.textContent : "";

        const done = () => {
            if (!button) return;
            button.textContent = "已复制";
            setTimeout(() => {
                button.textContent = original;
            }, 1200);
        };

        // 1. 异步剪贴板：只在安全上下文（HTTPS / localhost）可用
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(text);
                done();
                return;
            } catch {
                /* 落到下一级 */
            }
        }

        // 2. 隐藏 textarea + execCommand
        const area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.top = "-1000px";
        document.body.appendChild(area);
        area.select();
        let ok = false;
        try {
            ok = document.execCommand("copy");
        } catch {
            ok = false;
        }
        area.remove();
        if (ok) {
            done();
            return;
        }

        // 3. 都不行就让用户手动选
        showBanner("这个浏览器不允许脚本复制，请长按选中文本后手动复制。");
    }

    // ── 历史记录（只存文本） ──────────────────────────────────────────────

    function loadHistory() {
        try {
            const raw = JSON.parse(localStorage.getItem(LS_HISTORY) || "[]");
            return Array.isArray(raw) ? raw : [];
        } catch {
            return [];
        }
    }

    function saveHistory(items) {
        let list = items;
        // 先按条数截断，再按总长度截断，都丢最旧的
        if (list.length > HISTORY_MAX_ITEMS) list = list.slice(-HISTORY_MAX_ITEMS);
        let total = list.reduce((sum, item) => sum + item.text.length, 0);
        while (list.length > 1 && total > HISTORY_MAX_CHARS) {
            total -= list[0].text.length;
            list = list.slice(1);
        }
        try {
            localStorage.setItem(LS_HISTORY, JSON.stringify(list));
        } catch {
            showBanner("浏览器存储已满，这条历史没有保存。");
        }
        return list;
    }

    function pushHistory(card) {
        const text = cardText(card);
        if (!text) return;
        const list = loadHistory();
        list.push({
            id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            ts: Date.now(),
            name: card.name,
            model: card.model,
            ms: card.elapsedMs,
            avg: averageConfidence(card),
            text,
        });
        saveHistory(list);
        renderHistory();
    }

    function renderHistory() {
        const list = loadHistory();
        dom.historyEmpty.hidden = list.length > 0;
        dom.historyList.replaceChildren(
            ...list
                .slice()
                .reverse()
                .map((item) => renderHistoryItem(item, list)),
        );
    }

    function renderHistoryItem(item, list) {
        const node = dom.historyTemplate.content.firstElementChild.cloneNode(true);
        const pick = (role) => node.querySelector(`[data-role="${role}"]`);

        pick("meta").textContent =
            `${formatTime(item.ts)} · ${item.model} · ${item.ms} ms · ` +
            `置信度 ${(item.avg * 100).toFixed(1)}%`;
        pick("text").textContent = item.text;

        const copyButton = pick("copy");
        copyButton.addEventListener("click", () => copyText(item.text, copyButton));

        const removeButton = pick("remove");
        removeButton.addEventListener("click", () => {
            const remaining = list.filter((entry) => entry.id !== item.id);
            saveHistory(remaining);
            renderHistory();
        });

        return node;
    }

    function historyAsText() {
        return loadHistory()
            .map((item) => {
                const head =
                    `## ${formatTime(item.ts)} ${item.model}` +
                    `（${item.ms} ms，置信度 ${(item.avg * 100).toFixed(1)}%）`;
                return `${head}\n${item.text}`;
            })
            .join("\n\n");
    }

    // ── 事件 ──────────────────────────────────────────────────────────────

    function bindEvents() {
        dom.dropzone.addEventListener("click", () => dom.fileInput.click());
        dom.dropzone.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                dom.fileInput.click();
            }
        });

        dom.fileInput.addEventListener("change", (event) => {
            addFiles(event.target.files);
            event.target.value = "";
        });
        dom.cameraInput.addEventListener("change", (event) => {
            addFiles(event.target.files);
            event.target.value = "";
        });
        dom.cameraBtn.addEventListener("click", () => dom.cameraInput.click());

        for (const type of ["dragenter", "dragover"]) {
            dom.dropzone.addEventListener(type, (event) => {
                event.preventDefault();
                dom.dropzone.classList.add("dragover");
            });
        }
        for (const type of ["dragleave", "drop"]) {
            dom.dropzone.addEventListener(type, () => {
                dom.dropzone.classList.remove("dragover");
            });
        }
        dom.dropzone.addEventListener("drop", (event) => {
            event.preventDefault();
            addFiles(event.dataTransfer ? event.dataTransfer.files : null);
        });

        // 粘贴图片：只有带图片时才接管，纯文本粘贴照常交给输入框
        document.addEventListener("paste", (event) => {
            const items = event.clipboardData ? event.clipboardData.items : null;
            if (!items) return;
            const files = [];
            for (const item of items) {
                if (item.kind === "file" && item.type.startsWith("image/")) {
                    const file = item.getAsFile();
                    if (file) files.push(file);
                }
            }
            if (files.length) {
                event.preventDefault();
                addFiles(files);
            }
        });

        dom.modelSelect.addEventListener("change", () => {
            localStorage.setItem(LS_MODEL, currentModel());
            dom.modelHint.textContent = modelsById.get(currentModel()) || "";
        });

        dom.tokenInput.addEventListener("change", () => {
            saveToken(dom.tokenInput.value);
            dom.tokenHint.textContent = currentToken()
                ? "token 已保存在本机浏览器。"
                : "服务未启用鉴权时留空即可。";
        });

        dom.clearBtn.addEventListener("click", () => {
            for (const card of state.cards) URL.revokeObjectURL(card.previewUrl);
            state.cards = [];
            showBanner("");
            render();
        });

        dom.copyAll.addEventListener("click", () => copyText(allText(), dom.copyAll));

        dom.exportTxt.addEventListener("click", () => {
            const body = state.cards
                .filter((card) => card.status === "ok")
                .map((card) => `# ${card.name}（${card.model}）\n${cardText(card)}`)
                .join("\n\n");
            if (body) downloadText(`ocr-${Date.now()}.txt`, body);
        });

        dom.exportHistory.addEventListener("click", () => {
            const body = historyAsText();
            if (body) downloadText(`ocr-history-${Date.now()}.txt`, body);
        });

        dom.clearHistory.addEventListener("click", () => {
            saveHistory([]);
            renderHistory();
        });
    }

    // ── 启动 ──────────────────────────────────────────────────────────────

    function main() {
        bindEvents();
        const token = currentToken();
        if (token) {
            dom.tokenInput.value = token;
            dom.tokenHint.textContent = "token 已保存在本机浏览器。";
        }
        render();
        renderHistory();
        void loadModels();
    }

    main();
})();
