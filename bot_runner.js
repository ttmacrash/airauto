const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const USER_DATA_DIR = path.join(__dirname, 'chrome_bot_profile');
const STATUS_FILE = path.join(__dirname, 'bot_status.json');

function updateStatus(state, message, details = {}) {
  const payload = {
    state, // 'RUNNING' | 'WAITING_LOGIN' | 'APPLYING' | 'COMPLETED' | 'ERROR'
    message,
    updatedAt: new Date().toISOString(),
    ...details,
  };
  fs.writeFileSync(STATUS_FILE, JSON.stringify(payload, null, 2), 'utf-8');
  console.log(`[BOT ${state}] ${message}`);
}

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function runBot() {
  const argsJson = process.argv[2];
  if (!argsJson) {
    updateStatus('ERROR', '세팅할 데이터 인자가 전달되지 않았습니다.');
    process.exit(1);
  }

  let config;
  try {
    config = JSON.parse(argsJson);
  } catch (err) {
    updateStatus('ERROR', '세팅 데이터 JSON 파싱 오류: ' + err.message);
    process.exit(1);
  }

  const { listingId, windows, mode } = config;
  // mode: 'LOGIN_ONLY' or 'APPLY_SETTINGS'

  updateStatus('RUNNING', '내 컴퓨터에서 전용 크롬 자동화 봇을 실행하고 있습니다...', {
    listingId,
    totalWindows: (windows || []).length,
    completedWindows: 0,
  });

  let context;
  try {
    context = await chromium.launchPersistentContext(USER_DATA_DIR, {
      executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      headless: false,
      viewport: { width: 1440, height: 900 },
      args: ['--disable-blink-features=AutomationControlled'],
    });

    const page = context.pages()[0] || (await context.newPage());

    // Capture any GraphQL mutations made on the calendar page so we can also replay them directly if needed
    let capturedHeaders = null;
    page.on('request', (req) => {
      const url = req.url();
      if (url.includes('/api/v3/') && req.headers()['x-airbnb-api-key']) {
        capturedHeaders = req.headers();
      }
    });

    const calendarUrl = `https://www.airbnb.co.kr/multicalendar/${listingId}`;
    updateStatus('RUNNING', `에어비앤비 호스트 달력(${listingId}) 페이지로 이동 중...`);
    await page.goto(calendarUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await sleep(3000);

    // Check if redirected to login
    if (page.url().includes('/login') || page.url().includes('authenticate')) {
      updateStatus(
        'WAITING_LOGIN',
        '열린 크롬 창에서 에어비앤비 호스트 계정으로 로그인해 주세요! (최초 1회만 로그인하면 다음부터는 자동 로그인됩니다)',
        { listingId }
      );

      // Wait up to 5 minutes for the user to log in and reach multicalendar or hosting page
      const startWait = Date.now();
      while (Date.now() - startWait < 300000) {
        await sleep(2000);
        const curUrl = page.url();
        if (
          curUrl.includes('/multicalendar') ||
          curUrl.includes('/hosting') ||
          curUrl.includes('/calendar')
        ) {
          break;
        }
      }

      if (!page.url().includes('/multicalendar')) {
        await page.goto(calendarUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
        await sleep(3000);
      }
    }

    if (mode === 'LOGIN_ONLY') {
      updateStatus('COMPLETED', '호스트 계정 연동(로그인 세션 저장)이 완료되었습니다! 이제 언제든 1클릭 자동 세팅이 가능합니다.');
      await sleep(3000);
      await context.close();
      process.exit(0);
    }

    updateStatus('APPLYING', '호스트 달력 접속 완료! 공실 구간별 최적 요금 자동 세팅을 시작합니다...', {
      listingId,
      totalWindows: windows.length,
      completedWindows: 0,
    });

    // Inject floating status overlay inside the browser so the user can watch the bot working live!
    await page.evaluate(() => {
      const banner = document.createElement('div');
      banner.id = 'airauto-bot-overlay';
      banner.style.cssText =
        'position:fixed;bottom:24px;left:24px;z-index:999999;background:linear-gradient(90deg,#e61e4d,#d70466);color:#fff;padding:16px 22px;border-radius:16px;box-shadow:0 12px 32px rgba(0,0,0,0.35);font-family:sans-serif;font-size:14px;font-weight:bold;max-width:440px;line-height:1.5;';
      banner.innerHTML = '🤖 AirAuto 자동 세팅 봇이 달력을 분석하고 요금을 수정하고 있습니다...';
      document.body.appendChild(banner);
    });

    const resultsLog = [];

    for (let i = 0; i < windows.length; i++) {
      const w = windows[i];
      const { startDate, endDate, targetPrice, dates } = w;
      const stepMsg = `[${i + 1}/${windows.length}] ${startDate} ~ ${endDate} 공실 구간 → 1박 ₩${Number(targetPrice).toLocaleString()} 자동 세팅 중...`;

      updateStatus('APPLYING', stepMsg, {
        listingId,
        totalWindows: windows.length,
        completedWindows: i,
        resultsLog,
      });

      await page.evaluate((msg) => {
        const el = document.getElementById('airauto-bot-overlay');
        if (el) el.innerHTML = `🤖 <div>${msg}</div>`;
      }, stepMsg);

      let appliedOnUi = false;

      // Strategy 1: Navigate directly to the date edit route or click the calendar date cell
      const targetDates = dates && dates.length ? dates : [startDate];
      for (const dStr of targetDates) {
        try {
          // Try finding the day cell in Airbnb multicalendar DOM via data-testid or aria-label or date attribute
          const daySelector = `[data-testid*="${dStr}"], [data-date="${dStr}"], [aria-label*="${dStr}"]`;
          const dayEl = await page.$(daySelector);
          if (dayEl) {
            await dayEl.scrollIntoViewIfNeeded();
            await dayEl.click();
            await sleep(1200);
            appliedOnUi = true;
          }
        } catch (e) {
          // ignore selector error
        }
      }

      // If direct date URL routing works on Airbnb multicalendar, also try opening the side panel via URL or input
      try {
        // Look for price input field in the right-hand side panel of multicalendar
        const priceInput = await page.$(
          'input[name*="price"], input[id*="price"], input[data-testid*="price-input"], input[inputmode="numeric"]'
        );
        if (priceInput) {
          await priceInput.click({ clickCount: 3 });
          await priceInput.fill(String(targetPrice));
          await sleep(800);

          // Look for Save button (저장 / Save)
          const saveBtn = await page.$(
            'button[type="submit"], button:has-text("저장"), button:has-text("Save")'
          );
          if (saveBtn) {
            await saveBtn.click();
            await sleep(1500);
            appliedOnUi = true;
          }
        }
      } catch (e) {
        // ignore
      }

      resultsLog.push({
        window: `${startDate} ~ ${endDate}`,
        targetPrice,
        status: appliedOnUi ? '자동 입력 완료' : '달력 패널 열람 완료 (확인 대기)',
      });

      await sleep(1500);
    }

    const doneMsg = `✅ 총 ${windows.length}개 공실 구간에 대한 자동 세팅 프로세스가 완료되었습니다! 열린 크롬 창에서 변경된 달력을 확인하실 수 있습니다.`;
    await page.evaluate((msg) => {
      const el = document.getElementById('airauto-bot-overlay');
      if (el) {
        el.style.background = 'linear-gradient(90deg,#059669,#10b981)';
        el.innerHTML = msg;
      }
    }, doneMsg);

    updateStatus('COMPLETED', doneMsg, {
      listingId,
      totalWindows: windows.length,
      completedWindows: windows.length,
      resultsLog,
    });

    // Keep browser open for 60 seconds so the user can inspect the calendar
    await sleep(60000);
    await context.close();
  } catch (err) {
    updateStatus('ERROR', '봇 실행 중 오류 발생: ' + err.message);
    if (context) {
      try {
        await context.close();
      } catch (e) {}
    }
    process.exit(1);
  }
}

runBot();
