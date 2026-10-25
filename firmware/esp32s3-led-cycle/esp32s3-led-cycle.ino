/*
 * ESP32-S3 板载 RGB 三色循环 + MSP-1 串口控制 — 固件 v1.1
 *
 * v1.1 变更（依据《MHS-网关能力规范-v0.9》§6 MSP-1，改造范围不含时序算法）：
 *   - 保留 v1.0 自动循环为默认 cycle 模式：上电即红→绿→蓝，每色 2s
 *   - 新增 MSP-1 命令解析（硬件 CDC）：
 *       CMD PING             → ACK PING {"uptime_ms":...,"fw":"1.1"}
 *       CMD GET chip_temp_c  → ACK GET chip_temp_c {"chip_temp_c":..}
 *       CMD GET led          → ACK GET led {"mode":..,"color":..,"period_ms":..}
 *       CMD SET led <json>   → ACK SET led {"ok":true}
 *       CMD RESET            → ACK RESET {"ok":true}，1s 内重启
 *     错误：ERR <name> <code> <msg>，01 unknown_cmd 02 bad_args 03 busy
 *           04 internal 05 unsupported（03/04/05 保留）
 *   - 启动横幅：MSP1 READY fw=1.1（网关据此识别设备上线）
 *   - SET led 参数口径与描述符 esp32s3-led-01 一致：
 *       mode ∈ {static, blink, cycle, off}（必填）
 *       color "#rrggbb"（static/blink 必填）
 *       period_ms 100..10000，缺省 2000
 *
 * 依据《ESP32-S3-LED-需求与设计规格-v1.0》：
 *   - 板子：乐鑫 ESP32-S3-N16R8，板载 1 颗 WS2812 RGB 灯珠
 *   - 框架：Arduino-ESP32 2.0.17，构建板型 ESP32S3 Dev Module
 *     （Flash Size 16MB / PSRAM: OPI / USB CDC On Boot: Enabled）
 *   - 驱动：内置 neopixelWrite()，无需第三方库
 *
 * 引脚说明（规格 2.3）：
 *   默认 GPIO48；若实机不亮，改 RGB_PIN 为 38（官方 DevKitC-1 v1.1 改动）。
 *   上电瞬间灯珠闪一下随机色属 WS2812 正常现象，不是 bug。
 */

#include <Arduino.h>

// ---------- 可配置常量（集中管理，便于修改） ----------
static const int      RGB_PIN    = 48;        // WS2812 数据脚；备选 38
static const bool     LED_LOG    = false;     // 自测开关：true 时换色/命令打印时间戳（正式版必须为 false）

static const char    *FW_VERSION = "1.1";
static const uint32_t DEFAULT_PERIOD_MS = 2000;
static const uint32_t MIN_PERIOD_MS     = 100;    // 描述符 period_ms 下限
static const uint32_t MAX_PERIOD_MS     = 10000;  // 描述符 period_ms 上限
static const size_t   MSP_LINE_MAX      = 256;    // MSP-1 单行 ≤256B

// 颜色顺序固定：红 → 绿 → 蓝（规格 2.4）
static const uint8_t COLORS[][3] = {
    {255, 0, 0},  // 红
    {0, 255, 0},  // 绿
    {0, 0, 255},  // 蓝
};
static const char *COLOR_NAMES[] = {"RED", "GREEN", "BLUE"};
static const size_t COLOR_COUNT  = sizeof(COLORS) / sizeof(COLORS[0]);

// ---------- LED 状态 ----------
enum LedMode : uint8_t { MODE_CYCLE, MODE_STATIC, MODE_BLINK, MODE_OFF };

static LedMode  ledMode    = MODE_CYCLE;              // 默认 cycle（v1.0 行为）
static uint8_t  staticColor[3] = {0, 255, 0};         // static/blink 用颜色
static uint32_t periodMs   = DEFAULT_PERIOD_MS;       // cycle/blink 周期
static size_t   colorIndex = 0;                       // cycle 当前颜色下标
static uint32_t stepStart  = 0;                       // 当前节拍起始时刻
static bool     blinkOn    = true;                    // blink 相位：亮/灭
static bool     bootLogged = false;                   // 启动横幅是否已打印

// ---------- MSP-1 串口行缓冲 ----------
static char     lineBuf[MSP_LINE_MAX + 1];   // 用本文件常量，勿用系统宏 LINE_MAX（含义不同）
static size_t   lineLen      = 0;
static bool     lineOverflow = false;

static void renderLed() {
    switch (ledMode) {
    case MODE_CYCLE:
        neopixelWrite(RGB_PIN, COLORS[colorIndex][0], COLORS[colorIndex][1],
                      COLORS[colorIndex][2]);
        break;
    case MODE_STATIC:
        neopixelWrite(RGB_PIN, staticColor[0], staticColor[1], staticColor[2]);
        break;
    case MODE_BLINK:
        if (blinkOn) {
            neopixelWrite(RGB_PIN, staticColor[0], staticColor[1], staticColor[2]);
        } else {
            neopixelWrite(RGB_PIN, 0, 0, 0);
        }
        break;
    case MODE_OFF:
        neopixelWrite(RGB_PIN, 0, 0, 0);
        break;
    }
}

static void mspAck(const char *name, const char *json) {
    Serial.printf("ACK %s %s\r\n", name, json);
    if (LED_LOG) Serial.printf("[MSP] ACK %s %s @ %lu ms\r\n", name, json,
                               (unsigned long)millis());
}

static void mspErr(const char *name, uint8_t code, const char *msg) {
    Serial.printf("ERR %s %02u %s\r\n", name, code, msg);
    if (LED_LOG) Serial.printf("[MSP] ERR %s %02u %s @ %lu ms\r\n", name, code,
                               msg, (unsigned long)millis());
}

// ---------- 极简 JSON 字段提取（扁平对象，够 MSP-1 参数集用，零依赖） ----------
// 字符串字段：成功时写入 out 并返回 true；键不存在或值为 null 返回 false。
static bool jsonGetString(const char *args, const char *key, char *out, size_t outLen) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(args, pat);
    if (!p) return false;
    p += strlen(pat);
    while (*p == ' ' || *p == '\t') p++;
    if (*p != ':') return false;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '"') return false;                 // null / 非字符串 → 视为无值
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < outLen) out[i++] = *p++;
    out[i] = '\0';
    return *p == '"';                            // 必须正常闭合
}

// 无符号整数字段：键不存在返回 false 且不动 *val。
static bool jsonGetUInt(const char *args, const char *key, uint32_t *val) {
    char buf[16];
    if (!jsonGetString(args, key, buf, sizeof(buf))) {
        // 允许裸数字
        char pat[32];
        snprintf(pat, sizeof(pat), "\"%s\"", key);
        const char *p = strstr(args, pat);
        if (!p) return false;
        p += strlen(pat);
        while (*p == ' ' || *p == '\t') p++;
        if (*p != ':') return false;
        p++;
        while (*p == ' ' || *p == '\t') p++;
        if (*p < '0' || *p > '9') return false;
        *val = (uint32_t)strtoul(p, nullptr, 10);
        return true;
    }
    if (buf[0] < '0' || buf[0] > '9') return false;
    *val = (uint32_t)strtoul(buf, nullptr, 10);
    return true;
}

static bool isHex(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}
static uint8_t hexVal(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return c - 'A' + 10;
}
// "#rrggbb" → RGB 三元组
static bool parseColor(const char *s, uint8_t rgb[3]) {
    if (!s || strlen(s) != 7 || s[0] != '#') return false;
    for (int i = 1; i <= 6; i++) {
        if (!isHex(s[i])) return false;
    }
    rgb[0] = hexVal(s[1]) * 16 + hexVal(s[2]);
    rgb[1] = hexVal(s[3]) * 16 + hexVal(s[4]);
    rgb[2] = hexVal(s[5]) * 16 + hexVal(s[6]);
    return true;
}

static const char *modeName(LedMode m) {
    switch (m) {
    case MODE_STATIC: return "static";
    case MODE_BLINK:  return "blink";
    case MODE_OFF:    return "off";
    default:          return "cycle";
    }
}

// 切换模式并立即生效
static void applySet(LedMode m, const uint8_t *color, uint32_t period) {
    ledMode    = m;
    periodMs   = period;
    if (color) memcpy(staticColor, color, 3);
    colorIndex = 0;
    blinkOn    = true;
    stepStart  = millis();
    renderLed();
}

// ---------- 命令处理 ----------
static void handleLine(char *line) {
    // 去首尾空白
    while (*line == ' ' || *line == '\t' || *line == '\r') line++;
    size_t n = strlen(line);
    while (n && (line[n - 1] == ' ' || line[n - 1] == '\t' || line[n - 1] == '\r')) {
        line[--n] = '\0';
    }
    if (n == 0) return;                       // 空行忽略
    if (strncmp(line, "CMD", 3) != 0 ||
        (line[3] != ' ' && line[3] != '\t' && line[3] != '\0')) {
        return;                               // 非 MSP-1 行（人调试输出）静默忽略
    }

    char *p = line + 3;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == '\0') { mspErr("?", 1, "unknown_cmd"); return; }

    // ---- 无参数命令：PING / RESET ----
    if (strncmp(p, "PING", 4) == 0 && (p[4] == '\0' || p[4] == ' ' || p[4] == '\t')) {
        char *args = p + 4;
        while (*args == ' ' || *args == '\t') args++;
        if (*args) { mspErr("PING", 2, "bad_args"); return; }
        char result[64];
        snprintf(result, sizeof(result), "{\"uptime_ms\":%lu,\"fw\":\"%s\"}",
                 (unsigned long)millis(), FW_VERSION);
        mspAck("PING", result);
        return;
    }
    if (strncmp(p, "RESET", 5) == 0 && (p[5] == '\0' || p[5] == ' ' || p[5] == '\t')) {
        char *args = p + 5;
        while (*args == ' ' || *args == '\t') args++;
        if (*args) { mspErr("RESET", 2, "bad_args"); return; }
        mspAck("RESET", "{\"ok\":true}");
        Serial.flush();
        delay(800);                           // ACK 落地后 1s 内重启
        ESP.restart();
        return;
    }

    // ---- GET chip_temp_c / GET led ----
    if (strncmp(p, "GET", 3) == 0 && (p[3] == ' ' || p[3] == '\t')) {
        char *q = p + 3;
        while (*q == ' ' || *q == '\t') q++;
        char *args = q;
        while (*args && *args != ' ' && *args != '\t') args++;
        if (*args) {
            *args = '\0';
            args++;
            while (*args == ' ' || *args == '\t') args++;
        }
        if (strcmp(q, "chip_temp_c") == 0) {
            if (*args) { mspErr("GET chip_temp_c", 2, "bad_args"); return; }
            float t = temperatureRead();      // S3 片内温度（°C）
            char result[48];
            snprintf(result, sizeof(result), "{\"chip_temp_c\":%.1f}", t);
            mspAck("GET chip_temp_c", result);
            return;
        }
        if (strcmp(q, "led") == 0) {
            if (*args) { mspErr("GET led", 2, "bad_args"); return; }
            char colorJson[16];
            if (ledMode == MODE_STATIC || ledMode == MODE_BLINK) {
                snprintf(colorJson, sizeof(colorJson), "\"#%02x%02x%02x\"",
                         staticColor[0], staticColor[1], staticColor[2]);
            } else {
                snprintf(colorJson, sizeof(colorJson), "null");
            }
            char result[96];
            snprintf(result, sizeof(result),
                     "{\"mode\":\"%s\",\"color\":%s,\"period_ms\":%lu}",
                     modeName(ledMode), colorJson, (unsigned long)periodMs);
            mspAck("GET led", result);
            return;
        }
        mspErr(q, 1, "unknown_cmd");          // GET 未知资源
        return;
    }

    // ---- SET led <json> ----
    if (strncmp(p, "SET", 3) == 0 && (p[3] == ' ' || p[3] == '\t')) {
        char *q = p + 3;
        while (*q == ' ' || *q == '\t') q++;
        char *args = q;
        while (*args && *args != ' ' && *args != '\t') args++;
        if (*args) {
            *args = '\0';
            args++;
            while (*args == ' ' || *args == '\t') args++;
        }
        if (strcmp(q, "led") != 0) { mspErr(q, 1, "unknown_cmd"); return; }
        if (*args == '\0') { mspErr("SET led", 2, "bad_args"); return; }

        char modeBuf[16];
        if (!jsonGetString(args, "mode", modeBuf, sizeof(modeBuf))) {
            mspErr("SET led", 2, "bad_args");
            return;
        }
        LedMode m;
        bool needColor = false;
        if      (strcmp(modeBuf, "static") == 0) { m = MODE_STATIC; needColor = true; }
        else if (strcmp(modeBuf, "blink")  == 0) { m = MODE_BLINK;  needColor = true; }
        else if (strcmp(modeBuf, "cycle")  == 0) { m = MODE_CYCLE; }
        else if (strcmp(modeBuf, "off")    == 0) { m = MODE_OFF; }
        else { mspErr("SET led", 2, "bad_args"); return; }

        uint8_t color[3] = {0, 0, 0};
        const uint8_t *colorPtr = nullptr;
        if (needColor) {
            char colorBuf[16];
            if (!jsonGetString(args, "color", colorBuf, sizeof(colorBuf)) ||
                !parseColor(colorBuf, color)) {
                mspErr("SET led", 2, "bad_args");   // color 缺失或非法（描述符 required_if）
                return;
            }
            colorPtr = color;
        }
        uint32_t period = DEFAULT_PERIOD_MS;        // 缺省 2000（描述符 default）
        if (strstr(args, "\"period_ms\"")) {
            if (!jsonGetUInt(args, "period_ms", &period) || period < MIN_PERIOD_MS ||
                period > MAX_PERIOD_MS) {
                mspErr("SET led", 2, "bad_args");   // 越界（描述符 100..10000）
                return;
            }
        }
        applySet(m, colorPtr, period);
        mspAck("SET led", "{\"ok\":true}");
        return;
    }

    // ---- 未知命令名：取首个 token 作 name 回 ERR ----
    char name[24];
    size_t i = 0;
    while (p[i] && p[i] != ' ' && p[i] != '\t' && i + 1 < sizeof(name)) {
        name[i] = p[i];
        i++;
    }
    name[i] = '\0';
    mspErr(name, 1, "unknown_cmd");
}

// ---------- MSP-1 串口读取（非阻塞） ----------
static void pollSerial() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n') {
            if (lineOverflow) {
                mspErr("?", 2, "line_too_long");    // 单行 >256B（协议层保护）
            } else {
                lineBuf[lineLen] = '\0';
                handleLine(lineBuf);
            }
            lineLen      = 0;
            lineOverflow = false;
            continue;
        }
        if (lineLen >= MSP_LINE_MAX) {
            lineOverflow = true;                    // 丢弃超长行剩余字符
            continue;
        }
        lineBuf[lineLen++] = c;
    }
}

void setup() {
    Serial.begin(115200);
    // 上电即进入第一种颜色（红），FR1：无需任何触发
    colorIndex = 0;
    stepStart  = millis();
    renderLed();
}

void loop() {
    // 启动横幅：串口连上后只打一次，循环过程不刷屏（FR5）
    if (!bootLogged && Serial) {
        Serial.printf("MSP1 READY fw=%s\r\n", FW_VERSION);   // 网关识别上线
        Serial.printf("ESP32-S3 LED 循环 v%s | WS2812 @GPIO%d | cycle 每色 %lu ms | 芯片 %s\r\n",
                      FW_VERSION, RGB_PIN, (unsigned long)periodMs,
                      ESP.getChipModel());
        bootLogged = true;
    }

    pollSerial();

    // 非阻塞轮询节拍：cycle 每色 period_ms；blink 的 period_ms 为全周期（亮灭各半，
    // 翻相周期 = periodMs/2，与描述符 period_ms 语义一致）
    uint32_t now = millis();
    if (ledMode == MODE_CYCLE) {
        if (now - stepStart >= periodMs) {
            stepStart = now;  // 用 now 而非累加，避免长时间运行后漂移补偿过头
            colorIndex = (colorIndex + 1) % COLOR_COUNT;
            renderLed();
        }
    } else if (ledMode == MODE_BLINK) {
        if (now - stepStart >= periodMs / 2) {
            stepStart = now;
            blinkOn = !blinkOn;
            renderLed();
        }
    }
}
