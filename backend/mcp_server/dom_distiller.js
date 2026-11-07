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
    // 8. 人机验证（CAPTCHA）识别：reCAPTCHA / hCaptcha / Cloudflare Turnstile / 挑战页 / 滑块
    //    只做信号收集，不做任何绕过；命中后由 Agent 暂停并交给人工处理
    const verifyReasons = [];
    const addVerifyReason = (reason) => {
        if (reason && !verifyReasons.includes(reason)) verifyReasons.push(reason);
    };

    // 8.1 验证 iframe（跨域，MCP 无法点击，必须人工操作）
    //     覆盖 reCAPTCHA / hCaptcha / Turnstile / Arkose / GeeTest / Cloudflare / Bing Turing 挑战页
    for (const frame of document.querySelectorAll('iframe')) {
        const src = frame.getAttribute('src') || '';
        const title = frame.getAttribute('title') || '';
        if (/recaptcha|hcaptcha|turnstile|challenges\.cloudflare\.com|arkose|funcaptcha|geetest|captcha|turing\/|challengepage|\/challenge/i.test(src)
            || /captcha|challenge|验证/i.test(title)) {
            addVerifyReason(`检测到验证 iframe: ${src.slice(0, 100) || title}`);
        }
    }

    // 8.2 常见验证容器 / 站点密钥元素
    //     #hip / #b_captcha 是 Bing 的 Human Interactive Proof 挑战容器
    const verifySelectors = [
        '.g-recaptcha', '.h-captcha', '.cf-turnstile',
        '#challenge-form', '#cf-challenge-running', '#captcha',
        '#b_captcha', '#hip', '[data-sitekey]',
        'iframe[src*="captcha" i]', 'iframe[src*="turing" i]',
        'iframe[id*="captcha" i]', 'iframe[id*="challenge" i]'
    ];
    for (const sel of verifySelectors) {
        if (document.querySelector(sel)) {
            addVerifyReason(`检测到验证组件: ${sel}`);
            break;
        }
    }

    // 8.3 页面标题（Cloudflare "Just a moment..." / Bing "最后一步" 等挑战页）
    if (/just a moment|attention required|请稍候|人机验证|captcha|最后一步|请解决|verify you are human/i.test(document.title || '')) {
        addVerifyReason(`页面标题疑似验证页: ${document.title}`);
    }

    // 8.4 当前 URL 直接命中挑战页路径（Bing: /turing/captcha/...）
    if (/turing\/captcha|challengepage|\/captcha\/|arkose|funcaptcha/i.test(window.location.href)) {
        addVerifyReason(`页面 URL 命中验证路径: ${window.location.href.slice(0, 100)}`);
    }

    // 8.5 页面正文中的强提示词（避免"请输入验证码"这类普通表单误判）
    const strongVerifyPatterns = [
        /verify (you are|that you are) (a )?human/i,
        /are you (a )?robot/i,
        /i'?m not a robot/i,
        /security check/i,
        /checking your browser/i,
        /确认您是真人/i,
        /请完成(以下)?验证/i,
        /人机验证/i,
        /拖动滑块/i,
        /请解决以下难题/i,
        /solve (this|the following) (puzzle|challenge)/i,
        /complete (the|this) (challenge|security check)/i,
        /unusual (traffic|activity)/i,
        /automated (requests|queries)/i,
        /异常流量/i,
        /访问(过于)?频繁/i,
        /请求过于频繁/i
    ];
    const bodyText = (document.body ? document.body.innerText : '').slice(0, 4000);
    if (bodyText) {
        for (const pattern of strongVerifyPatterns) {
            if (pattern.test(bodyText)) {
                addVerifyReason(`页面正文命中验证提示: ${pattern.source}`);
                break;
            }
        }
    }

    // 返回精简后的页面摘要，供大模型阅读
    return {
        url: window.location.href,
        title: document.title,
        interactiveCount: distilledItems.length,
        distilledDOM: distilledItems.join('\n'),
        verification: {
            detected: verifyReasons.length > 0,
            reasons: verifyReasons
        }
    };
})();
