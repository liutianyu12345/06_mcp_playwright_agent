(() => {
    // 1. 清理上一次注入留下的红框标记与旧编号（避免重复叠加）
    document.querySelectorAll('.mcp-som-overlay').forEach(el => el.remove());
    document.querySelectorAll('[data-mcp-id]').forEach(el => el.removeAttribute('data-mcp-id'));

    // 2. 定义什么是"可交互元素"的选择器
    const interactiveSelectors = [
        "a[href]",
        "button",
        'input:not([type="hidden"])',
        'textarea',
        'select',
        '[role="button"]',
        '[role="link"]',
        '[role="menuitem"]',
        '[onclick]'
    ].join(',');

    const rawElements = Array.from(document.querySelectorAll(interactiveSelectors));
    const distilledItems = [];
    let indexCounter = 1;

    // 3. 遍历并过滤掉隐藏、被遮挡或无意义的节点
    for (const el of rawElements) {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);

        // 剔除宽高为0、透明、隐藏的元素
        if (
            rect.width < 5 ||
            rect.height < 5 ||
            style.display === 'none' ||
            style.visibility === 'hidden' ||
            parseFloat(style.opacity) === 0
        ) {
            continue;
        }
        // 只保留当前屏幕可视区域（Viewport）附近的元素
        if (rect.bottom < 0 || rect.top > window.innerHeight) {
            continue;
        }

        // 4. 提取该元素的关键语义信息（剔除冗余 class/style）
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        const text = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 50);
        const placeholder = el.getAttribute('placeholder') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';

        // 如果一个按钮既没文字也没提示，大概率是无意义图标，跳过
        if (!text && !placeholder && !ariaLabel && tag !== 'input') {
            continue;
        }

        // 5. 生成唯一数字索引标记（Set-of-Mark），写回真实 DOM 属性方便后续点击
        const somId = indexCounter++;
        el.setAttribute('data-mcp-id', String(somId));

        // 6. 在网页上绘制可视化红框编号（截图给前端或视觉大模型看时非常直观）
        const badge = document.createElement('div');
        badge.className = 'mcp-som-overlay';
        badge.textContent = `[${somId}]`;
        Object.assign(badge.style, {
            position: 'fixed',
            left: `${Math.max(0, rect.left)}px`,
            top: `${Math.max(0, rect.top)}px`,
            backgroundColor: 'rgba(220, 38, 38, 0.85)',
            color: '#fff',
            fontSize: '11px',
            fontWeight: 'bold',
            padding: '1px 4px',
            borderRadius: '3px',
            zIndex: '2147483647',
            pointerEvents: 'none' // 不阻挡鼠标点击
        });
        document.body.appendChild(badge);

        // 7. 组装极简描述行，例如: [3] <input type="text" placeholder="搜索...">
        let desc = `[${somId}] <${tag}`;
        if (type) desc += ` type="${type}"`;
        if (placeholder) desc += ` placeholder="${placeholder}"`;
        if (ariaLabel) desc += ` aria="${ariaLabel}"`;
        desc += `>${text}</${tag}>`;

        distilledItems.push(desc);
    }
    // 返回精简后的页面摘要，供大模型阅读
    return {
        url: window.location.href,
        title: document.title,
        interactiveCount: distilledItems.length,
        distilledDOM: distilledItems.join('\n')
    };
})();
