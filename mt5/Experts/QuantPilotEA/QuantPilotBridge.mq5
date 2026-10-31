//+------------------------------------------------------------------+
//| QuantPilotBridge.mq5                                             |
//| QuantPilot AI - MT5 EA bridge: read-only telemetry + command      |
//| channel with OBSERVE_ONLY / DRY_RUN / LIVE(demo) run modes.        |
//|                                                                   |
//| What this EA does (and ALL it does in this slice):               |
//|   - reports terminal/account identity to the local agent          |
//|   - reports symbol trading specifications (PRD 5.4)                |
//|   - sends doc A.3 account heartbeats on a timer                  |
//|   - sends open-position snapshots (doc 12.2 projection, read-only)|
//|   - sends pending-order snapshots (doc 12.2 orders projection)   |
//|   - sends account history deals incrementally (read-only)        |
//|   - sends the MT5 built-in economic calendar (read-only)         |
//|   - sends closed bars for the configured timeframes (read-only;  |
//|     PRD 7 market-data input for the cloud strategy engine)       |
//|   - polls the cloud-side account config every 60s and LOGS it    |
//|     (informational only - it is never applied, OBSERVE_ONLY)     |
//|   - polls the cloud command channel and ACKS each command:        |
//|     receipt (DUPLICATE/EXPIRED/OBSERVE_ONLY_ACK), DRY_RUN pre-      |
//|     trade checks, or live CTrade execution on DEMO accounts        |
//|     only (slice 3, behind the LiveTrading input; PRD 21.3)         |
//|   - persists a crash-safe execution journal for live mutating     |
//|     commands: verdict + broker tickets hit disk before the ack;   |
//|     a startup scan re-sends owed acks and flags interrupted       |
//|     commands EXECUTE_UNRESOLVED - never a blind local re-run      |
//|   - sends a shutdown note on removal                             |
//|                                                                   |
//| Command channel (PRD 13.5 + AT-005/AT-006):                       |
//|   - the EA polls /ea/v1/commands/poll every CommandPollSeconds   |
//|   - every received command is marked processed BEFORE the ack is |
//|     sent (terminal GlobalVariable "QPCMD_<id>", survives EA      |
//|     reloads); a lost ack is still reconcilable via the           |
//|     recently_acked echo on the next poll (AT-006)                 |
//|   - R1 crash-safety: live commands that mutate broker state get  |
//|     a disk journal (QuantPilot\cmdstate\<id>.json) BEFORE the     |
//|     processed mark; the verdict + broker tickets are persisted    |
//|     BEFORE the ack POST; the startup/periodic scan re-sends owed |
//|     acks and acks interrupted commands EXECUTE_UNRESOLVED for   |
//|     cloud-side reconciliation - an interrupted command is NEVER |
//|     re-executed locally (no blind retries, acceptance C3)        |
//|   - duplicate commands get ack_code=DUPLICATE, no side effects    |
//|   - commands past expires_at get ack_code=EXPIRED - expired        |
//|     commands are NEVER executed, in any mode (acceptance C3)      |
//|                                                                   |
//| Run modes (three-level ladder, acceptance report section 5):      |
//|   - OBSERVE_ONLY (default): DryRunMode=false, LiveTrading=false;  |
//|     every command is acked, nothing is executed                   |
//|   - DRY_RUN (C1): DryRunMode=true; risk-increasing commands       |
//|     (OPEN_MARKET / PLACE_PENDING) run the full pre-trade chain    |
//|     (trade mode, spread, volume, SL/TP distance, pending price,   |
//|     session + OrderCheck oracle) and ack DRY_RUN_PASSED /         |
//|     DRY_RUN_REJECTED. DryRunMode=true NEVER sends an order - it   |
//|     short-circuits live execution too.                            |
//|   - LIVE (C2): DryRunMode=false AND LiveTrading=true AND the      |
//|     account is DEMO (per-command hard guard). Risk-increasing     |
//|     commands run the SAME C1 chain first, then one synchronous   |
//|     CTrade attempt; acks EXECUTED / EXECUTE_FAILED /             |
//|     EXECUTE_REJECTED / LIVE_REFUSED_NOT_DEMO. Single attempt, no  |
//|     auto-retry (the cloud re-issues under its own policy, C3).    |
//|     MODIFY_POSITION never removes protection: absent sl/tp mean   |
//|     "keep current" (PRD 13.4).                                    |
//|                                                                   |
//| What it MUST NOT do (PRD 21.3):                                   |
//|   - never execute on a REAL/CONTEST account: per-command hard    |
//|     guard acks LIVE_REFUSED_NOT_DEMO, independent of inputs        |
//|   - never send an order while DryRunMode=true                    |
//|   - no autonomous decision: no cloud command = no action; it      |
//|     never acts on responses other than logging / acking them       |
//|                                                                   |
//| Identity verification (PRD 6.3): every payload carries the same   |
//| terminal + account identity; the cloud flags IDENTITY_MISMATCH.   |
//| Account switching inside the terminal re-triggers registration.   |
//+------------------------------------------------------------------+
#property copyright "QuantPilot AI"
#property version   "1.80"
#property strict

#include <QuantPilot\QpJson.mqh>
#include <Trade\Trade.mqh>

#define QP_EA_VERSION        "1.9.0"
#define QP_HTTP_TIMEOUT_MS  5000
#define QP_SYMBOLS_PER_BATCH 25
#define QP_SPEC_REFRESH_SEC  3600   // re-report full specs hourly
#define QP_DEALS_PER_BATCH   200
#define QP_DEALS_REFRESH_SEC 60    // incremental history-deal poll
#define QP_BARS_PER_BATCH    100   // closed bars per /ea/v1/bars batch
#define QP_CONFIG_REFRESH_SEC 60   // config poll (informational only)
#define QP_CALENDAR_REFRESH_SEC 600 // re-report the calendar window (10 min)
#define QP_CALENDAR_PER_BATCH 200
#define QP_CALENDAR_PAST_SEC  7200  // include releases from the past 2h
#define QP_CMD_RECENT_MAX     50    // recently_acked echo ring size (AT-006)
#define QP_ACK_OBSERVE_ONLY   "OBSERVE_ONLY_ACK"
#define QP_ACK_DUPLICATE      "DUPLICATE"
#define QP_ACK_EXPIRED        "EXPIRED"
#define QP_ACK_DRY_RUN_PASS   "DRY_RUN_PASSED"
#define QP_ACK_DRY_RUN_REJECT "DRY_RUN_REJECTED"
#define QP_ACK_EXECUTED       "EXECUTED"
#define QP_ACK_EXECUTE_FAIL   "EXECUTE_FAILED"
#define QP_ACK_EXECUTE_REJ    "EXECUTE_REJECTED"
#define QP_ACK_NOT_DEMO       "LIVE_REFUSED_NOT_DEMO"
#define QP_ACK_UNRESOLVED     "EXECUTE_UNRESOLVED"   // interrupted execution: reconcile, never blind-retry (R1)
#define QP_CMDSTATE_DIR       "QuantPilot\\cmdstate\\" // per-command execution journals (R1)
#define QP_CMDSTATE_RETENTION_SEC 604800              // prune delivered journals after 7 days

//--- inputs (PRD 5.7 subset relevant to this slice)
input string AgentEndpoint    = "http://127.0.0.1:8787"; // Local agent base URL (loopback only)
input string AgentToken       = "";                     // Shared local token (X-QP-Token)
input int    HeartbeatSeconds = 10;                     // Heartbeat interval, 5..15s (PRD 5.7)
input int    MaxSymbols       = 100;                    // Max Market Watch symbols to report
input int    CalendarWindowHours = 48;                  // Economic calendar lookahead (hours)
input int    CommandPollSeconds = 5;                    // Command poll interval, 1..120s
input bool   DryRunMode       = false;                  // DRY_RUN: pre-trade checks only, never execute (PRD 13.5 / C1)
input int    MaxSpreadPoints  = 0;                      // DRY_RUN: max allowed spread in points (0 = no spread check)
input bool   LiveTrading      = false;                  // LIVE: real CTrade execution, DEMO accounts only (acceptance C2)
input double MaxLiveVolume    = 0.0;                    // LIVE: local cap on order volume, rejects fatter orders (0 = off)
input string BarTimeframes   = "M15,H1";                // Closed-bar timeframes: M5/M15/M30/H1/H4 (PRD 22.2)
input int    BarHistoryCount = 500;                     // Initial closed-bar backfill per timeframe, 50..2000
input string LogLevel         = "INFO";                 // DEBUG | INFO | ERROR

//--- live trading (CTrade, slice 3 / acceptance C2)
CTrade   g_trade;

//--- runtime state
bool     g_registered      = false;
string   g_last_login      = "";
string   g_last_server     = "";
string   g_terminal_json    = "";
datetime g_last_specs_at   = 0;
datetime g_last_deal_time  = 0;    // max deal time already reported (0 = full backfill)
datetime g_last_deals_at   = 0;    // when the deals channel last succeeded
datetime g_last_config_at  = 0;    // when the config poll last succeeded
datetime g_last_calendar_at = 0;  // when the calendar report last succeeded
datetime g_last_command_at = 0;   // when the command poll last succeeded
datetime g_last_cmdstate_scan = 0; // when the journal scan last ran (R1)
string   g_recent_command_ids[]; // recently acked command_ids (echo ring)

//--- closed-bar reporting state (one slot per configured timeframe)
int         g_bar_count = 0;     // number of parsed BarTimeframes entries
ENUM_TIMEFRAMES g_bar_tf[];     // parsed timeframe enums
string      g_bar_labels[];     // labels sent to the cloud ("M15","H1",...)
datetime    g_bar_last_closed[]; // max closed-bar open time reported (0 = backfill)

//+------------------------------------------------------------------+
//| Logging                                                          |
//+------------------------------------------------------------------+
void QpLog(const string level, const string message)
  {
   if(LogLevel == "ERROR" && level != "ERROR")
      return;
   if(LogLevel == "INFO" && level == "DEBUG")
      return;
   Print("[QP][", level, "] ", message);
  }

//+------------------------------------------------------------------+
//| ISO-8601 UTC timestamp from TimeGMT()                            |
//+------------------------------------------------------------------+
string QpIsoNow()
  {
   string s = TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS); // "2026.10.30 12:00:00"
   StringReplace(s, ".", "-");
   StringReplace(s, " ", "T");
   return(s + "Z");
  }

//+------------------------------------------------------------------+
//| Enum labels (MQL5 reference values, no invention - PRD 21.3)     |
//+------------------------------------------------------------------+
string QpTradeModeLabel(const long mode)
  {
   switch((int)mode)
     {
      case SYMBOL_TRADE_MODE_DISABLED:  return("DISABLED");
      case SYMBOL_TRADE_MODE_LONGONLY:  return("LONGONLY");
      case SYMBOL_TRADE_MODE_SHORTONLY: return("SHORTONLY");
      case SYMBOL_TRADE_MODE_CLOSEONLY: return("CLOSEONLY");
      case SYMBOL_TRADE_MODE_FULL:      return("FULL");
     }
   return("UNKNOWN_" + IntegerToString(mode));
  }

string QpFillingModes(const long mask)
  {
   string out = "";
   if((mask & SYMBOL_FILLING_FOK) != 0)
      out += (out == "" ? "" : ",") + "\"FOK\"";
   if((mask & SYMBOL_FILLING_IOC) != 0)
      out += (out == "" ? "" : ",") + "\"IOC\"";
   return("[" + out + "]");
  }

string QpAccountModeLabel(const long mode)
  {
   switch((int)mode)
     {
      case ACCOUNT_TRADE_MODE_DEMO:    return("DEMO");
      case ACCOUNT_TRADE_MODE_CONTEST: return("CONTEST");
      case ACCOUNT_TRADE_MODE_REAL:    return("REAL");
     }
   return("UNKNOWN");
  }

//+------------------------------------------------------------------+
//| Run mode label (phase-3 P2: the single mode definition, mirrored |
//| by contracts.derive_ea_run_mode). Reported in every heartbeat so |
//| the cloud/UI show the REAL mode instead of a hardcoded label.   |
//| DryRunMode takes precedence over LiveTrading (QpHandleCommand    |
//| short-circuits the same way).                                    |
//+------------------------------------------------------------------+
string QpRunMode()
  {
   if(DryRunMode)
      return("DRY_RUN");
   if(LiveTrading)
      return("LIVE");
   return("OBSERVE_ONLY");
  }

string QpMarginModeLabel(const long mode)
  {
   switch((int)mode)
     {
      case ACCOUNT_MARGIN_MODE_RETAIL_NETTING: return("RETAIL_NETTING");
      case ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: return("RETAIL_HEDGING");
      case ACCOUNT_MARGIN_MODE_EXCHANGE:        return("EXCHANGE");
     }
   return("UNKNOWN");
  }

//+------------------------------------------------------------------+
//| Identity fragments (PRD 6.3)                                     |
//+------------------------------------------------------------------+
string QpTerminalJson()
  {
   QpJsonObject o;
   o.Clear();
   o.AddInt("build", TerminalInfoInteger(TERMINAL_BUILD));
   o.AddStr("company", TerminalInfoString(TERMINAL_COMPANY));
   o.AddStr("exe_path", TerminalInfoString(TERMINAL_PATH));
   o.AddStr("data_path", TerminalInfoString(TERMINAL_DATA_PATH));
   return(o.Dump());
  }

string QpAccountJson()
  {
   QpJsonObject o;
   o.Clear();
   o.AddStr("login_ref", IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)));
   o.AddStr("server", AccountInfoString(ACCOUNT_SERVER));
   o.AddStr("company", AccountInfoString(ACCOUNT_COMPANY));
   o.AddStr("currency", AccountInfoString(ACCOUNT_CURRENCY));
   o.AddInt("leverage", AccountInfoInteger(ACCOUNT_LEVERAGE));
   o.AddStr("margin_mode", QpMarginModeLabel(AccountInfoInteger(ACCOUNT_MARGIN_MODE)));
   o.AddStr("account_mode", QpAccountModeLabel(AccountInfoInteger(ACCOUNT_TRADE_MODE)));
   return(o.Dump());
  }

bool QpIdentityChanged()
  {
   string login  = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string server = AccountInfoString(ACCOUNT_SERVER);
   return(login != g_last_login || server != g_last_server);
  }

//+------------------------------------------------------------------+
//| HTTP POST to the local agent (loopback only)                     |
//+------------------------------------------------------------------+
bool QpHttpPost(const string path, const string payload)
  {
   string response_body;
   return(QpHttpPost(path, payload, response_body));
  }

bool QpHttpPost(const string path, const string payload, string &response_body)
  {
   string url     = AgentEndpoint + path;
   string headers = "Content-Type: application/json\r\n" +
                    "X-QP-Token: " + AgentToken + "\r\n";
   char data[];
   char result_data[];
   string result_headers;

   StringToCharArray(payload, data, 0, StringLen(payload), CP_UTF8);
   int status = WebRequest("POST", url, headers, QP_HTTP_TIMEOUT_MS,
                           data, result_data, result_headers);
   if(status == -1)
     {
      int err = GetLastError();
      if(err == 4014)
         QpLog("ERROR", "WebRequest blocked: add '" + AgentEndpoint +
               "' to Tools > Options > Expert Advisors > Allow WebRequest URLs");
      else
         QpLog("ERROR", "WebRequest failed to " + url + " err=" + IntegerToString(err));
      return(false);
     }
   if(status != 200)
     {
      QpLog("ERROR", "agent returned HTTP " + IntegerToString(status) + " for " + path);
      return(false);
     }
   string body = CharArrayToString(result_data, 0, ArraySize(result_data), CP_UTF8);
   response_body = body;
   if(StringFind(body, "\"ok\":true") < 0 && StringFind(body, "\"ok\": true") < 0)
     {
      QpLog("ERROR", "agent rejected " + path + ": " + body);
      return(false);
     }
   QpLog("DEBUG", "agent accepted " + path + " (" + IntegerToString(StringLen(payload)) + " bytes)");
   return(true);
  }

//+------------------------------------------------------------------+
//| Registration (terminal + account identity)                       |
//+------------------------------------------------------------------+
bool QpSendRegister()
  {
   g_terminal_json = QpTerminalJson();
   string account  = QpAccountJson();

   g_last_login  = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   g_last_server = AccountInfoString(ACCOUNT_SERVER);

   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", account);
   o.AddStr("observed_at", QpIsoNow());
   return(QpHttpPost("/ea/v1/register", o.Dump()));
  }

//+------------------------------------------------------------------+
//| Heartbeat (doc A.3 structure + identity tuple)                    |
//+------------------------------------------------------------------+
bool QpSendHeartbeat()
  {
   QpJsonObject o;
   o.Clear();
   o.AddStr("node_id", ""); // assigned by the agent/cloud; identity tuple carries resolution
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", QpAccountJson());
   o.AddBool("connected", TerminalInfoInteger(TERMINAL_CONNECTED) != 0);
   o.AddBool("trade_allowed", AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) != 0);
   o.AddBool("expert_allowed", AccountInfoInteger(ACCOUNT_TRADE_EXPERT) != 0);
   o.AddNumStr("balance", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   o.AddNumStr("equity", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
   o.AddNumStr("margin", DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2));
   o.AddNumStr("free_margin", DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2));
   o.AddStr("currency", AccountInfoString(ACCOUNT_CURRENCY));
   o.AddInt("open_positions", PositionsTotal());
   o.AddStr("run_mode", QpRunMode());
   o.AddStr("observed_at", QpIsoNow());
   return(QpHttpPost("/ea/v1/heartbeat", o.Dump()));
  }

//+------------------------------------------------------------------+
//| ISO-8601 UTC timestamp from an arbitrary datetime                 |
//+------------------------------------------------------------------+
string QpIsoTime(const datetime t)
  {
   string s = TimeToString(t, TIME_DATE | TIME_SECONDS);
   StringReplace(s, ".", "-");
   StringReplace(s, " ", "T");
   return(s + "Z");
  }

//+------------------------------------------------------------------+
//| Open positions snapshot (doc 12.2 projection, read-only)         |
//| PositionGetTicket(i) selects position i; no trade calls anywhere.|
//+------------------------------------------------------------------+
bool QpSendPositions()
  {
   string positions_json = "";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      string name = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(name, SYMBOL_DIGITS);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);

      QpJsonObject p;
      p.Clear();
      p.AddStr("broker_position_ticket", IntegerToString((long)ticket));
      p.AddStr("broker_symbol", name);
      p.AddStr("direction", PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL");
      p.AddNumStr("volume", DoubleToString(PositionGetDouble(POSITION_VOLUME), 8));
      p.AddNumStr("price_open", DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), digits));
      p.AddNumStr("current_price", DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), digits));
      if(sl != 0.0)
         p.AddNumStr("sl", DoubleToString(sl, digits));
      if(tp != 0.0)
         p.AddNumStr("tp", DoubleToString(tp, digits));
      p.AddNumStr("profit", DoubleToString(PositionGetDouble(POSITION_PROFIT), 2));
      p.AddNumStr("swap", DoubleToString(PositionGetDouble(POSITION_SWAP), 2));
      p.AddStr("open_time", QpIsoTime((datetime)PositionGetInteger(POSITION_TIME)));

      if(i > 0)
         positions_json += ",";
      positions_json += p.Dump();
     }

   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", QpAccountJson());
   o.AddRaw("positions", "[" + positions_json + "]");
   o.AddStr("observed_at", QpIsoNow());
   if(!QpHttpPost("/ea/v1/positions", o.Dump()))
      return(false);
   QpLog("DEBUG", "reported " + IntegerToString(total) + " open position(s)");
   return(true);
  }

//+------------------------------------------------------------------+
//| Enum labels for history deals / pending orders (MQL5 reference)  |
//+------------------------------------------------------------------+
string QpDealEntryLabel(const long entry)
  {
   switch((int)entry)
     {
      case DEAL_ENTRY_IN:     return("IN");
      case DEAL_ENTRY_OUT:    return("OUT");
      case DEAL_ENTRY_INOUT:  return("INOUT");
      case DEAL_ENTRY_OUT_BY: return("OUT_BY");
     }
   return("UNKNOWN");
  }

string QpOrderKindLabel(const long otype)
  {
   switch((int)otype)
     {
      case ORDER_TYPE_BUY_LIMIT:        return("BUY_LIMIT");
      case ORDER_TYPE_SELL_LIMIT:       return("SELL_LIMIT");
      case ORDER_TYPE_BUY_STOP:         return("BUY_STOP");
      case ORDER_TYPE_SELL_STOP:        return("SELL_STOP");
      case ORDER_TYPE_BUY_STOP_LIMIT:   return("BUY_STOP_LIMIT");
      case ORDER_TYPE_SELL_STOP_LIMIT:  return("SELL_STOP_LIMIT");
     }
   return("UNKNOWN_" + IntegerToString(otype));
  }

string QpOrderDirection(const long otype)
  {
   switch((int)otype)
     {
      case ORDER_TYPE_BUY_LIMIT:
      case ORDER_TYPE_BUY_STOP:
      case ORDER_TYPE_BUY_STOP_LIMIT:
         return("BUY");
      case ORDER_TYPE_SELL_LIMIT:
      case ORDER_TYPE_SELL_STOP:
      case ORDER_TYPE_SELL_STOP_LIMIT:
         return("SELL");
     }
   return(""); // not a pending BUY/SELL-family order; caller skips it
  }

//+------------------------------------------------------------------+
//| Incremental account history deals (read-only, doc 12.2).          |
//| HistorySelect + HistoryDealGet* only; no trade calls anywhere.   |
//| Sends deals with time >= g_last_deal_time; the inclusive bound    |
//| re-sends boundary-second deals whose ticket arrived late - the   |
//| cloud upserts idempotently on (account_id, broker_deal_ticket).  |
//+------------------------------------------------------------------+
bool QpSendDeals()
  {
   if(!HistorySelect(0, TimeCurrent() + 86400))
     {
      QpLog("ERROR", "HistorySelect failed");
      return(false);
     }
   int total = HistoryDealsTotal();

   string parts[];
   int count = 0;
   datetime max_time = g_last_deal_time;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      long dtype = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL)
         continue; // skip balance/credit operations - market fills only
      datetime dtime = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      if(g_last_deal_time > 0 && dtime < g_last_deal_time)
         continue;

      string name = HistoryDealGetString(ticket, DEAL_SYMBOL);
      int digits = (int)SymbolInfoInteger(name, SYMBOL_DIGITS);
      QpJsonObject d;
      d.Clear();
      d.AddStr("broker_deal_ticket", IntegerToString((long)ticket));
      d.AddStr("broker_order_ticket", IntegerToString((long)HistoryDealGetInteger(ticket, DEAL_ORDER)));
      d.AddStr("broker_position_ticket", IntegerToString((long)HistoryDealGetInteger(ticket, DEAL_POSITION_ID)));
      d.AddStr("broker_symbol", name);
      d.AddStr("direction", dtype == DEAL_TYPE_BUY ? "BUY" : "SELL");
      d.AddStr("entry", QpDealEntryLabel(HistoryDealGetInteger(ticket, DEAL_ENTRY)));
      d.AddNumStr("volume", DoubleToString(HistoryDealGetDouble(ticket, DEAL_VOLUME), 8));
      d.AddNumStr("price", DoubleToString(HistoryDealGetDouble(ticket, DEAL_PRICE), digits));
      d.AddNumStr("commission", DoubleToString(HistoryDealGetDouble(ticket, DEAL_COMMISSION), 2));
      d.AddNumStr("swap", DoubleToString(HistoryDealGetDouble(ticket, DEAL_SWAP), 2));
      d.AddNumStr("profit", DoubleToString(HistoryDealGetDouble(ticket, DEAL_PROFIT), 2));
      d.AddStr("deal_time", QpIsoTime(dtime));
      d.AddInt("magic", HistoryDealGetInteger(ticket, DEAL_MAGIC));
      d.AddStr("comment", HistoryDealGetString(ticket, DEAL_COMMENT));
      int n = ArraySize(parts);
      ArrayResize(parts, n + 1);
      parts[n] = d.Dump();
      count++;
      if(dtime > max_time)
         max_time = dtime;
     }

   if(count == 0)
     {
      QpLog("DEBUG", "no new deals since last report");
      return(true);
     }

   int batch_total = (count + QP_DEALS_PER_BATCH - 1) / QP_DEALS_PER_BATCH;
   bool all_ok = true;
   int sent = 0;
   for(int batch_index = 0; batch_index < batch_total; batch_index++)
     {
      int from = batch_index * QP_DEALS_PER_BATCH;
      int to = from + QP_DEALS_PER_BATCH;
      if(to > count)
         to = count;
      string deals_json = "";
      for(int j = from; j < to; j++)
        {
         if(j > from)
            deals_json += ",";
         deals_json += parts[j];
        }
      QpJsonObject o;
      o.Clear();
      o.AddStr("ea_version", QP_EA_VERSION);
      o.AddRaw("terminal", g_terminal_json);
      o.AddRaw("account", QpAccountJson());
      o.AddInt("batch_index", batch_index);
      o.AddInt("batch_total", batch_total);
      o.AddRaw("deals", "[" + deals_json + "]");
      o.AddStr("observed_at", QpIsoNow());
      if(!QpHttpPost("/ea/v1/deals", o.Dump()))
        {
         all_ok = false;
         break;
        }
      sent += to - from;
     }

   if(all_ok)
     {
      g_last_deal_time = max_time;
      QpLog("INFO", "reported " + IntegerToString(sent) + " deal(s) in " +
            IntegerToString(batch_total) + " batch(es)");
     }
   return(all_ok);
  }

//+------------------------------------------------------------------+
//| Closed-bar reporting (PRD 7 market-data input, read-only).        |
//| One channel per configured timeframe (BarTimeframes input).      |
//| bar_time is the bar OPEN time in broker-server time, matching    |
//| the deals channel convention (documented approximation).         |
//| Only CLOSED bars are sent (shift >= 1); the cloud upserts them   |
//| idempotently and evaluates strategies on the newest one.         |
//+------------------------------------------------------------------+
bool QpTimeframeToEnum(const string label, ENUM_TIMEFRAMES &tf)
  {
   if(label == "M5")  { tf = PERIOD_M5;  return(true); }
   if(label == "M15") { tf = PERIOD_M15; return(true); }
   if(label == "M30") { tf = PERIOD_M30; return(true); }
   if(label == "H1")  { tf = PERIOD_H1;  return(true); }
   if(label == "H4")  { tf = PERIOD_H4;  return(true); }
   return(false);
  }

bool QpInitBarTimeframes()
  {
   g_bar_count = 0;
   ArrayResize(g_bar_tf, 0);
   ArrayResize(g_bar_labels, 0);
   ArrayResize(g_bar_last_closed, 0);

   string labels[];
   int n = StringSplit(BarTimeframes, ',', labels);
   for(int i = 0; i < n; i++)
     {
      string label = labels[i];
      StringTrimLeft(label);
      StringTrimRight(label);
      if(label == "")
         continue;
      ENUM_TIMEFRAMES tf;
      if(!QpTimeframeToEnum(label, tf))
        {
         QpLog("ERROR", "unsupported BarTimeframes entry '" + label +
               "' (allowed: M5,M15,M30,H1,H4)");
         return(false);
        }
      bool dup = false;
      for(int j = 0; j < g_bar_count; j++)
         if(g_bar_labels[j] == label)
            dup = true;
      if(dup)
         continue;
      ArrayResize(g_bar_tf, g_bar_count + 1);
      ArrayResize(g_bar_labels, g_bar_count + 1);
      ArrayResize(g_bar_last_closed, g_bar_count + 1);
      g_bar_tf[g_bar_count] = tf;
      g_bar_labels[g_bar_count] = label;
      g_bar_last_closed[g_bar_count] = 0; // 0 = full backfill on first report
      g_bar_count++;
     }

   if(g_bar_count == 0)
     {
      QpLog("ERROR", "BarTimeframes must list at least one of M5,M15,M30,H1,H4");
      return(false);
     }
   return(true);
  }

void QpResetBarBackfill()
  {
   for(int i = 0; i < g_bar_count; i++)
      g_bar_last_closed[i] = 0;
  }

bool QpSendBarsFor(const int idx)
  {
   ENUM_TIMEFRAMES tf = g_bar_tf[idx];
   datetime newest_closed = iTime(_Symbol, tf, 1);
   if(newest_closed == 0)
     {
      QpLog("DEBUG", "no bar data yet for " + _Symbol + " " + g_bar_labels[idx]);
      return(true); // history not synced yet; retried on the next tick
     }

   int count = 0;
   if(g_bar_last_closed[idx] == 0)
     {
      // initial backfill: the most recent closed bars (shift 0 is forming)
      int avail = Bars(_Symbol, tf) - 1;
      if(avail <= 0)
         return(true);
      count = (BarHistoryCount < avail ? BarHistoryCount : avail);
     }
   else if(newest_closed == g_bar_last_closed[idx])
      return(true); // no new closed bar since the last report
   else
     {
      int last_shift = iBarShift(_Symbol, tf, g_bar_last_closed[idx], true);
      if(last_shift < 0)
        {
         // the last reported bar vanished (history resync) -> backfill again
         QpLog("INFO", "bar history resync on " + _Symbol + " " +
               g_bar_labels[idx] + "; backfilling");
         g_bar_last_closed[idx] = 0;
         return(QpSendBarsFor(idx));
        }
      count = last_shift - 1; // closed bars newer than the last reported one
      if(count <= 0)
        {
         g_bar_last_closed[idx] = newest_closed;
         return(true);
        }
      if(count > BarHistoryCount)
         count = BarHistoryCount; // bound the catch-up payload
     }

   MqlRates rates[];
   int copied = CopyRates(_Symbol, tf, 1, count, rates); // shift 1 = newest CLOSED bar
   if(copied <= 0)
     {
      QpLog("DEBUG", "CopyRates failed for " + _Symbol + " " + g_bar_labels[idx] +
            " err=" + IntegerToString(GetLastError()));
      return(false);
     }

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   string parts[];
   ArrayResize(parts, copied);
   datetime max_time = 0;
   for(int i = 0; i < copied; i++)
     {
      QpJsonObject b;
      b.Clear();
      b.AddStr("bar_time", QpIsoTime(rates[i].time));
      b.AddNumStr("open", DoubleToString(rates[i].open, digits));
      b.AddNumStr("high", DoubleToString(rates[i].high, digits));
      b.AddNumStr("low", DoubleToString(rates[i].low, digits));
      b.AddNumStr("close", DoubleToString(rates[i].close, digits));
      b.AddInt("tick_volume", rates[i].tick_volume);
      parts[i] = b.Dump();
      if(rates[i].time > max_time)
         max_time = rates[i].time;
     }

   int batch_total = (copied + QP_BARS_PER_BATCH - 1) / QP_BARS_PER_BATCH;
   for(int batch_index = 0; batch_index < batch_total; batch_index++)
     {
      int from = batch_index * QP_BARS_PER_BATCH;
      int to = from + QP_BARS_PER_BATCH;
      if(to > copied)
         to = copied;
      string bars_json = "";
      for(int j = from; j < to; j++)
        {
         if(j > from)
            bars_json += ",";
         bars_json += parts[j];
        }
      QpJsonObject o;
      o.Clear();
      o.AddStr("ea_version", QP_EA_VERSION);
      o.AddRaw("terminal", g_terminal_json);
      o.AddRaw("account", QpAccountJson());
      o.AddStr("broker_symbol", _Symbol);
      o.AddStr("timeframe", g_bar_labels[idx]);
      o.AddInt("batch_index", batch_index);
      o.AddInt("batch_total", batch_total);
      o.AddRaw("bars", "[" + bars_json + "]");
      o.AddStr("observed_at", QpIsoNow());
      if(!QpHttpPost("/ea/v1/bars", o.Dump()))
         return(false); // state not advanced -> the same window retries
     }

   g_bar_last_closed[idx] = max_time;
   QpLog("INFO", "reported " + IntegerToString(copied) + " closed bar(s) on " +
         _Symbol + " " + g_bar_labels[idx]);
   return(true);
  }

bool QpSendBars()
  {
   bool all_ok = true;
   for(int idx = 0; idx < g_bar_count; idx++)
     {
      if(!QpSendBarsFor(idx))
         all_ok = false;
     }
   return(all_ok);
  }

//+------------------------------------------------------------------+
//| Pending orders snapshot (doc 12.2 orders projection, read-only).  |
//| OrdersTotal/OrderGetTicket/OrderGet* only; no trade calls.        |
//| Same state-sync contract as positions: the snapshot is the       |
//| complete set of active pending orders at observed_at.            |
//+------------------------------------------------------------------+
bool QpSendOrders()
  {
   string orders_json = "";
   int total = OrdersTotal();
   int added = 0;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      long otype = OrderGetInteger(ORDER_TYPE);
      string direction = QpOrderDirection(otype);
      if(direction == "")
        {
         QpLog("DEBUG", "skipping order " + IntegerToString((long)ticket) +
               " with unsupported type " + IntegerToString(otype));
         continue;
        }
      string name = OrderGetString(ORDER_SYMBOL);
      int digits = (int)SymbolInfoInteger(name, SYMBOL_DIGITS);
      double sl = OrderGetDouble(ORDER_SL);
      double tp = OrderGetDouble(ORDER_TP);

      QpJsonObject od;
      od.Clear();
      od.AddStr("broker_ticket", IntegerToString((long)ticket));
      od.AddStr("broker_symbol", name);
      od.AddStr("direction", direction);
      od.AddStr("order_kind", QpOrderKindLabel(otype));
      od.AddNumStr("volume", DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 8));
      od.AddNumStr("price", DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), digits));
      if(sl != 0.0)
         od.AddNumStr("sl", DoubleToString(sl, digits));
      if(tp != 0.0)
         od.AddNumStr("tp", DoubleToString(tp, digits));
      od.AddStr("time_setup", QpIsoTime((datetime)OrderGetInteger(ORDER_TIME_SETUP)));
      long expiry = OrderGetInteger(ORDER_TIME_EXPIRATION);
      if(expiry != 0)
         od.AddStr("time_expiration", QpIsoTime((datetime)expiry));
      if(added > 0)
         orders_json += ",";
      orders_json += od.Dump();
      added++;
     }

   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", QpAccountJson());
   o.AddRaw("orders", "[" + orders_json + "]");
   o.AddStr("observed_at", QpIsoNow());
   if(!QpHttpPost("/ea/v1/orders", o.Dump()))
      return(false);
   QpLog("DEBUG", "reported " + IntegerToString(added) + " pending order(s)");
   return(true);
  }

//+------------------------------------------------------------------+
//| Cloud config poll (informational only, PRD 21.3 OBSERVE_ONLY).   |
//| The agent resolves this identity to the account config in the    |
//| cloud and relays it back. The EA only LOGS the response - it is   |
//| never applied to any behavior; this build contains no trading    |
//| logic whatsoever.                                                 |
//+------------------------------------------------------------------+
bool QpSendConfigPoll()
  {
   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", QpAccountJson());
   o.AddStr("observed_at", QpIsoNow());
   string body = "";
   if(!QpHttpPost("/ea/v1/config", o.Dump(), body))
      return(false);
   // OBSERVE_ONLY: informational log only; nothing here influences trading
   QpLog("INFO", "config poll: " + (StringLen(body) > 0 ? body : "(empty response)"));
   return(true);
  }

//+------------------------------------------------------------------+
//| Minimal JSON field extraction (response parsing only).           |
//| QpJson.mqh builds JSON; these helpers read the few scalar fields |
//| the command poll response carries. Slice 1 acks used strings +  |
//| integers only; slice 2 DRY_RUN also extracts the nested payload  |
//| object and its volume/price doubles - read-only, for validation. |
//+------------------------------------------------------------------+
bool QpJsonValueStart(const string json, const string key, int &pos)
  {
   pos = StringFind(json, "\"" + key + "\"");
   if(pos < 0)
      return(false);
   pos += StringLen(key) + 2;
   int len = StringLen(json);
   // skip whitespace, expect ':', skip whitespace again
   while(pos < len && (StringGetCharacter(json, pos) == ' ' ||
                       StringGetCharacter(json, pos) == '\t' ||
                       StringGetCharacter(json, pos) == '\n' ||
                       StringGetCharacter(json, pos) == '\r'))
      pos++;
   if(pos >= len || StringGetCharacter(json, pos) != ':')
      return(false);
   pos++;
   while(pos < len && (StringGetCharacter(json, pos) == ' ' ||
                       StringGetCharacter(json, pos) == '\t' ||
                       StringGetCharacter(json, pos) == '\n' ||
                       StringGetCharacter(json, pos) == '\r'))
      pos++;
   return(pos < len);
  }

string QpJsonGetString(const string json, const string key)
  {
   int pos;
   if(!QpJsonValueStart(json, key, pos))
      return("");
   if(StringGetCharacter(json, pos) != '"')
      return(""); // non-string value
   pos++;
   string out = "";
   int len = StringLen(json);
   while(pos < len)
     {
      ushort c = StringGetCharacter(json, pos);
      if(c == '\\')
        {
         pos++;
         if(pos >= len)
            break;
         ushort e = StringGetCharacter(json, pos);
         if(e == '"')
            out += "\"";
         else if(e == '\\')
            out += "\\";
         else if(e == 'n')
            out += "\n";
         else if(e == 'r')
            out += "\r";
         else if(e == 't')
            out += "\t";
         else if(e == 'u')
           {
            out += "\\u" + StringSubstr(json, pos + 1, 4); // passthrough
            pos += 4;
           }
         pos++;
         continue;
        }
      if(c == '"')
         break;
      out += StringSubstr(json, pos, 1);
      pos++;
     }
   return(out);
  }

long QpJsonGetLong(const string json, const string key)
  {
   int pos;
   if(!QpJsonValueStart(json, key, pos))
      return(0);
   int len = StringLen(json);
   string token = "";
   while(pos < len)
     {
      ushort c = StringGetCharacter(json, pos);
      if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\n' || c == '\r')
         break;
      token += StringSubstr(json, pos, 1);
      pos++;
     }
   return(StringToInteger(token));
  }

// extract a numeric field as double (DRY_RUN payloads carry volume /
// price / stop_loss / take_profit; Money fields arrive as decimal
// strings per doc 13.1, so quoted and bare numbers are both accepted)
double QpJsonGetDouble(const string json, const string key)
  {
   int pos;
   if(!QpJsonValueStart(json, key, pos))
      return(0.0);
   int len = StringLen(json);
   bool quoted = (StringGetCharacter(json, pos) == '"');
   if(quoted)
      pos++;
   string token = "";
   while(pos < len)
     {
      ushort c = StringGetCharacter(json, pos);
      if(quoted)
        {
         if(c == '"')
            break;
        }
      else if(c == ',' || c == '}' || c == ']' || c == ' ' || c == '\n' || c == '\r')
         break;
      token += StringSubstr(json, pos, 1);
      pos++;
     }
   return(StringToDouble(token));
  }

// extract the raw text of array "key" (bracket-balanced, string-aware)
bool QpJsonExtractArray(const string json, const string key, string &out)
  {
   out = "";
   int pos;
   if(!QpJsonValueStart(json, key, pos))
      return(false);
   if(StringGetCharacter(json, pos) != '[')
      return(false);
   int len = StringLen(json);
   int depth = 0;
   bool in_string = false;
   int start = pos;
   while(pos < len)
     {
      ushort c = StringGetCharacter(json, pos);
      if(in_string)
        {
         if(c == '\\')
            pos++; // skip escaped char
         else if(c == '"')
            in_string = false;
        }
      else
        {
         if(c == '"')
            in_string = true;
         else if(c == '[')
            depth++;
         else if(c == ']')
           {
            depth--;
            if(depth == 0)
              {
               out = StringSubstr(json, start, pos - start + 1);
               return(true);
              }
           }
        }
      pos++;
     }
   return(false);
  }

// extract the raw text of nested object "key" (brace-balanced, string-aware)
bool QpJsonExtractObject(const string json, const string key, string &out)
  {
   out = "";
   int pos;
   if(!QpJsonValueStart(json, key, pos))
      return(false);
   if(StringGetCharacter(json, pos) != '{')
      return(false);
   int len = StringLen(json);
   int depth = 0;
   bool in_string = false;
   int start = pos;
   while(pos < len)
     {
      ushort c = StringGetCharacter(json, pos);
      if(in_string)
        {
         if(c == '\\')
            pos++; // skip escaped char
         else if(c == '"')
            in_string = false;
        }
      else
        {
         if(c == '"')
            in_string = true;
         else if(c == '{')
            depth++;
         else if(c == '}')
           {
            depth--;
            if(depth == 0)
              {
               out = StringSubstr(json, start, pos - start + 1);
               return(true);
              }
           }
        }
      pos++;
     }
   return(false);
  }

// split a JSON array of objects into individual object strings
int QpJsonSplitObjects(const string array_json, string &objs[])
  {
   ArrayResize(objs, 0);
   int len = StringLen(array_json);
   int depth = 0;
   bool in_string = false;
   int obj_start = -1;
   for(int pos = 0; pos < len; pos++)
     {
      ushort c = StringGetCharacter(array_json, pos);
      if(in_string)
        {
         if(c == '\\')
            pos++;
         else if(c == '"')
            in_string = false;
         continue;
        }
      if(c == '"')
         in_string = true;
      else if(c == '{')
        {
         if(depth == 0)
            obj_start = pos;
         depth++;
        }
      else if(c == '}')
        {
         depth--;
         if(depth == 0 && obj_start >= 0)
           {
            int n = ArraySize(objs);
            ArrayResize(objs, n + 1);
            objs[n] = StringSubstr(array_json, obj_start, pos - obj_start + 1);
            obj_start = -1;
           }
        }
     }
   return(ArraySize(objs));
  }

//+------------------------------------------------------------------+
//| Command dedup (AT-005): terminal-level GlobalVariable marker.    |
//| GlobalVariables persist across EA reloads within the terminal,  |
//| so a re-attached EA still recognizes commands it already acked.|
//+------------------------------------------------------------------+
bool QpCommandProcessed(const string command_id)
  {
   return(GlobalVariableCheck("QPCMD_" + command_id));
  }

void QpMarkCommandProcessed(const string command_id)
  {
   GlobalVariableSet("QPCMD_" + command_id, (double)TimeGMT());
  }

// remember command_id for the recently_acked echo (AT-006 reconciliation)
void QpRememberCommand(const string command_id)
  {
   int n = ArraySize(g_recent_command_ids);
   if(n < QP_CMD_RECENT_MAX)
     {
      ArrayResize(g_recent_command_ids, n + 1);
      g_recent_command_ids[n] = command_id;
      return;
     }
   for(int i = 0; i < n - 1; i++)
      g_recent_command_ids[i] = g_recent_command_ids[i + 1];
   g_recent_command_ids[n - 1] = command_id;
  }

string QpRecentCommandsJson()
  {
   string out = "";
   int n = ArraySize(g_recent_command_ids);
   for(int i = 0; i < n; i++)
     {
      if(i > 0)
         out += ",";
      out += "\"" + g_recent_command_ids[i] + "\"";
     }
   return("[" + out + "]");
  }

//+------------------------------------------------------------------+
//| Crash-safe command execution journal (phase-3 R1).               |
//| A LIVE command that mutates broker state is journaled EXECUTING  |
//| on disk BEFORE the processed mark and the CTrade call; the final  |
//| verdict + broker tickets are persisted BEFORE the ack POST; the   |
//| acked flag right after it. A crash can therefore never silently   |
//| lose a verdict: the scan re-sends owed acks and flags interrupted |
//| commands EXECUTE_UNRESOLVED - it NEVER re-executes them locally;  |
//| the cloud reconciles those against broker projections before any  |
//| operator re-issue.                                                |
//+------------------------------------------------------------------+
#define QP_CMDSTATE_EXECUTING "EXECUTING"
#define QP_CMDSTATE_FINAL     "FINAL"

// command types that mutate broker state in LIVE mode - only these get
// a journal (receipts and DRY_RUN verdicts stay memoryless as before)
bool QpCommandMutatesBrokerState(const string command_type)
  {
   return(command_type == "OPEN_MARKET" || command_type == "PLACE_PENDING" ||
          command_type == "MODIFY_POSITION" || command_type == "CLOSE_POSITION" ||
          command_type == "CANCEL_ORDER");
  }

struct QpCommandState
  {
   string command_id;
   string command_type;
   string state;             // EXECUTING | FINAL
   string ack_code;          // final verdict ("" while EXECUTING)
   string ack_message;
   string order_ticket;      // "0" = none / keep stored value
   string deal_ticket;
   string position_ticket;
   bool   acked;             // ack POST reached the local agent
   long   updated_unix;
  };

string QpCommandStatePath(const string command_id)
  {
   return(QP_CMDSTATE_DIR + command_id + ".json");
  }

bool QpCommandStateExists(const string command_id)
  {
   return(FileIsExist(QpCommandStatePath(command_id)));
  }

void QpCommandStateWrite(const string command_id, const QpCommandState &state)
  {
   QpJsonObject o;
   o.Clear();
   o.AddStr("command_id", state.command_id);
   o.AddStr("command_type", state.command_type);
   o.AddStr("state", state.state);
   o.AddStr("ack_code", state.ack_code);
   o.AddStr("ack_message", state.ack_message);
   o.AddStr("order_ticket", state.order_ticket);
   o.AddStr("deal_ticket", state.deal_ticket);
   o.AddStr("position_ticket", state.position_ticket);
   o.AddInt("acked", state.acked ? 1 : 0);
   o.AddInt("updated_unix", state.updated_unix);
   int handle = FileOpen(QpCommandStatePath(command_id),
                        FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
     {
      QpLog("ERROR", "cmdstate write failed for " + command_id +
            " (err=" + IntegerToString(GetLastError()) +
            "); journal NOT on disk - ack will still be attempted");
      return;
     }
   FileWriteString(handle, o.Dump());
   FileFlush(handle);
   FileClose(handle);
  }

bool QpCommandStateLoad(const string command_id, QpCommandState &state)
  {
   int handle = FileOpen(QpCommandStatePath(command_id),
                        FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return(false);
   string json = FileReadString(handle);
   FileClose(handle);
   state.command_id      = command_id;
   state.command_type    = QpJsonGetString(json, "command_type");
   state.state           = QpJsonGetString(json, "state");
   state.ack_code        = QpJsonGetString(json, "ack_code");
   state.ack_message     = QpJsonGetString(json, "ack_message");
   state.order_ticket    = QpJsonGetString(json, "order_ticket");
   state.deal_ticket     = QpJsonGetString(json, "deal_ticket");
   state.position_ticket = QpJsonGetString(json, "position_ticket");
   state.acked           = (QpJsonGetLong(json, "acked") != 0);
   state.updated_unix    = QpJsonGetLong(json, "updated_unix");
   if(state.state == "")
      state.state = QP_CMDSTATE_EXECUTING; // torn write -> assume the worst
   return(true);
  }

// journal a live mutating command as EXECUTING BEFORE the processed mark
// and the CTrade call - a crash anywhere past this point is recoverable
void QpCommandStateInit(const string command_id, const string command_type)
  {
   if(QpCommandStateExists(command_id))
      return; // recovery / duplicate delivery: never re-init a journal
   QpCommandState state;
   state.command_id      = command_id;
   state.command_type    = command_type;
   state.state           = QP_CMDSTATE_EXECUTING;
   state.ack_code        = "";
   state.ack_message     = "";
   state.order_ticket    = "0";
   state.deal_ticket     = "0";
   state.position_ticket = "0";
   state.acked           = false;
   state.updated_unix    = (long)TimeGMT();
   QpCommandStateWrite(command_id, state);
  }

// persist the final verdict + broker tickets BEFORE the ack POST (a lost
// ack must never lose the broker outcome). Tickets of 0 keep the stored
// value; an already-delivered (acked) journal is never overwritten.
void QpCommandStateSetOutcome(const string command_id, const string ack_code,
                              const string ack_message, const long order_ticket,
                              const long deal_ticket, const long position_ticket)
  {
   QpCommandState state;
   if(!QpCommandStateLoad(command_id, state))
      return; // no journal (receipt / DRY_RUN paths): nothing to persist
   if(state.acked)
      return; // verdict already delivered - keep the stored record intact
   state.state         = QP_CMDSTATE_FINAL;
   state.ack_code      = ack_code;
   state.ack_message   = ack_message;
   if(order_ticket > 0)
      state.order_ticket = IntegerToString(order_ticket);
   if(deal_ticket > 0)
      state.deal_ticket = IntegerToString(deal_ticket);
   if(position_ticket > 0)
      state.position_ticket = IntegerToString(position_ticket);
   state.acked        = false; // outcome known, ack still owed
   state.updated_unix = (long)TimeGMT();
   QpCommandStateWrite(command_id, state);
  }

void QpCommandStateSetAcked(const string command_id, const bool sent)
  {
   QpCommandState state;
   if(!QpCommandStateLoad(command_id, state))
      return;
   state.acked        = sent;
   state.updated_unix = (long)TimeGMT();
   QpCommandStateWrite(command_id, state);
  }

//+------------------------------------------------------------------+
//| Command ack: receipt / DRY_RUN verdict / live-execution outcome   |
//| Broker tickets ride along on live outcomes (cloud reconciliation |
//| material, R1). The verdict is journaled BEFORE the POST and the   |
//| acked flag right after it - a lost ack is re-sent by the journal  |
//| scan, never silently dropped.                                    |
//+------------------------------------------------------------------+
bool QpSendCommandAck(const string command_id, const string ack_code,
                      const string ack_message,
                      const long order_ticket = 0, const long deal_ticket = 0,
                      const long position_ticket = 0)
  {
   QpCommandStateSetOutcome(command_id, ack_code, ack_message,
                            order_ticket, deal_ticket, position_ticket);
   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", QpAccountJson());
   o.AddStr("command_id", command_id);
   o.AddStr("ack_code", ack_code);
   o.AddStr("ack_message", ack_message);
   if(order_ticket > 0)
      o.AddStr("order_ticket", IntegerToString(order_ticket));
   if(deal_ticket > 0)
      o.AddStr("deal_ticket", IntegerToString(deal_ticket));
   if(position_ticket > 0)
      o.AddStr("position_ticket", IntegerToString(position_ticket));
   o.AddStr("observed_at", QpIsoNow());
   bool sent = QpHttpPost("/ea/v1/commands/ack", o.Dump());
   QpCommandStateSetAcked(command_id, sent);
   return(sent);
  }

//+------------------------------------------------------------------+
//| Journal recovery (R1): an interrupted or unacknowledged command.  |
//| EXECUTING = the crash straddled the CTrade call - the broker      |
//| outcome is UNKNOWN: ack EXECUTE_UNRESOLVED so the cloud can      |
//| reconcile against broker state. NEVER re-execute locally.        |
//| FINAL + !acked = verdict recorded but never delivered: re-send    |
//| the stored verdict verbatim (idempotent on the cloud side).       |
//| Returns true when the journal owed an ack (callers stop here).    |
//+------------------------------------------------------------------+
bool QpCommandStateRecover(const string command_id)
  {
   QpCommandState state;
   if(!QpCommandStateLoad(command_id, state))
      return(false);
   if(state.state == QP_CMDSTATE_EXECUTING)
     {
      QpLog("WARN", "cmdstate recover: " + command_id + " (" + state.command_type +
            ") was interrupted mid-execution - acking " + QP_ACK_UNRESOLVED +
            "; the cloud must reconcile the broker outcome, NO local re-execution");
      QpRememberCommand(command_id);
      QpSendCommandAck(command_id, QP_ACK_UNRESOLVED,
                       "EA restarted while " + state.command_type + " was executing; " +
                       "outcome uncertain - confirm against broker state before any re-issue");
      return(true);
     }
   if(!state.acked)
     {
      QpLog("WARN", "cmdstate recover: re-sending stored verdict " + state.ack_code +
            " for " + command_id + " (previous ack undelivered)");
      QpRememberCommand(command_id);
      QpSendCommandAck(command_id, state.ack_code, state.ack_message,
                       StringToInteger(state.order_ticket),
                       StringToInteger(state.deal_ticket),
                       StringToInteger(state.position_ticket));
      return(true);
     }
   return(false); // final and delivered: nothing owed
  }

// "<uuid>.json" (with or without a folder prefix) -> command id
string QpCommandIdFromFilename(const string filename)
  {
   string name = filename;
   if(StringLen(name) > 5 && StringFind(name, ".json") == StringLen(name) - 5)
      name = StringSubstr(name, 0, StringLen(name) - 5);
   for(int i = StringLen(name) - 1; i >= 0; i--)
      if(StringGetCharacter(name, i) == '\\')
        {
         name = StringSubstr(name, i + 1);
         break;
        }
   return(name);
  }

// Scan the journal directory: prune fully-acked journals past retention,
// back-fill dedup marks wiped by a terminal restart, and recover every
// journal that still owes an ack (e.g. the local agent was down). Runs
// once in OnInit and at most every 60s from OnTimer.
void QpCommandStateScan()
  {
   string filename;
   long   find = FileFindFirst(QP_CMDSTATE_DIR + "*.json", filename);
   if(find == INVALID_HANDLE)
      return;
   string names[];
   while(filename != "")
     {
      int n = ArraySize(names);
      ArrayResize(names, n + 1);
      names[n] = filename;
      if(!FileFindNext(find, filename))
         break;
     }
   FileFindClose(find);

   int recovered = 0;
   for(int i = 0; i < ArraySize(names); i++)
     {
      string id = QpCommandIdFromFilename(names[i]);
      if(id == "")
         continue;
      QpCommandState state;
      if(!QpCommandStateLoad(id, state))
         continue;
      if(state.acked &&
         (long)TimeGMT() - state.updated_unix >= QP_CMDSTATE_RETENTION_SEC)
        {
         FileDelete(QpCommandStatePath(id)); // delivered long ago: prune
         continue;
        }
      if(!QpCommandProcessed(id))
        {
         // terminal restart wiped the GlobalVariables - restore the dedup
         // mark so a re-delivered command cannot execute twice
         QpMarkCommandProcessed(id);
         if(state.acked)
            QpRememberCommand(id); // already delivered: echo ring only
        }
      if(!state.acked && QpCommandStateRecover(id))
         recovered++;
     }
   if(recovered > 0)
      QpLog("WARN", "cmdstate scan: recovered " + IntegerToString(recovered) +
            " unacknowledged command journal(s)");
  }

//+------------------------------------------------------------------+
//| Risk classification: only OPEN_MARKET / PLACE_PENDING add risk    |
//| (PRD 13.5). The C1 chain below gates both DRY_RUN (never a trade) |
//| and LIVE execution (same gates, then one CTrade attempt).        |
//| Chain: trade mode -> quotes/spread -> volume -> SL/TP distance -> |
//| pending price -> session -> OrderCheck margin/validity oracle.   |
//| A clean chain acks DRY_RUN_PASSED / executes; any failure acks   |
//| DRY_RUN_REJECTED / EXECUTE_REJECTED with a machine-readable code.|
//+------------------------------------------------------------------+
bool QpIsRiskIncreasing(const string command_type)
  {
   return(command_type == "OPEN_MARKET" || command_type == "PLACE_PENDING");
  }

bool QpOrderKindToType(const string order_kind, ENUM_ORDER_TYPE &type)
  {
   if(order_kind == "BUY_LIMIT")
     { type = ORDER_TYPE_BUY_LIMIT; return(true); }
   if(order_kind == "SELL_LIMIT")
     { type = ORDER_TYPE_SELL_LIMIT; return(true); }
   if(order_kind == "BUY_STOP")
     { type = ORDER_TYPE_BUY_STOP; return(true); }
   if(order_kind == "SELL_STOP")
     { type = ORDER_TYPE_SELL_STOP; return(true); }
   return(false);
  }

// trade-session check: SymbolInfoSessionTrade for today and yesterday
// (sessions may cross midnight); "00:00 - 00:00" means around the clock.
// If the symbol publishes no session info at all, stay lenient here and
// let the final OrderCheck() be the authority.
bool QpSessionOpen(const string symbol)
  {
   datetime server_now = TimeTradeServer();
   if(server_now == 0)
      return(true);
   MqlDateTime parts;
   TimeToStruct(server_now, parts);
   int now_sec = parts.hour * 3600 + parts.min * 60 + parts.sec;
   bool has_sessions = false;
   for(int d = 0; d < 2; d++) // 0 = today, 1 = yesterday (crossing midnight)
     {
      int check_dow = (parts.day_of_week - d + 7) % 7;
      int now_shifted = now_sec + d * 86400;
      for(uint session_index = 0; session_index < 16; session_index++)
        {
         datetime from = 0, to = 0;
         if(!SymbolInfoSessionTrade(symbol, (ENUM_DAY_OF_WEEK)check_dow,
                                    session_index, from, to))
            break;
         int from_sec = (int)((long)from % 86400);
         int to_sec = (int)((long)to % 86400);
         if(from_sec == to_sec)
           {
            if(from_sec == 0)
               return(true); // 00:00 - 00:00 on a listed weekday = 24h
            has_sessions = true;
            continue; // zero-length session
           }
         has_sessions = true;
         int end_sec = to_sec;
         if(end_sec < from_sec)
            end_sec += 86400; // session crosses midnight
         if(now_shifted >= from_sec && now_shifted < end_sec)
            return(true);
        }
     }
   return(!has_sessions); // no session info published -> lenient
  }

// full C1 pre-trade chain; on success detail carries the pass summary,
// on failure code carries the machine-readable rejection reason.
bool QpPreTradeChecks(const string cmd_json, const string command_type,
                      string &code, string &detail)
  {
   code = "";
   detail = "";

   //--- payload (OpenMarketPayload / PlacePendingPayload fields)
   string payload_json;
   if(!QpJsonExtractObject(cmd_json, "payload", payload_json) || payload_json == "")
     {
      code = "PAYLOAD_MISSING";
      detail = "command carries no payload object";
      return(false);
     }
   string symbol = QpJsonGetString(payload_json, "symbol");
   string side = QpJsonGetString(payload_json, "side");
   double volume = QpJsonGetDouble(payload_json, "volume");
   double sl = QpJsonGetDouble(payload_json, "stop_loss");
   double tp = QpJsonGetDouble(payload_json, "take_profit");
   double price = QpJsonGetDouble(payload_json, "price"); // pending only
   string order_kind = QpJsonGetString(payload_json, "order_kind");
   long deviation = QpJsonGetLong(payload_json, "max_deviation_points");
   long magic = QpJsonGetLong(payload_json, "magic_number");

   if(symbol == "" || side == "" || volume <= 0.0)
     {
      code = "PAYLOAD_INCOMPLETE";
      detail = "payload needs symbol, side and a positive volume";
      return(false);
     }
   bool is_buy = (side == "BUY");
   if(!is_buy && side != "SELL")
     {
      code = "BAD_SIDE";
      detail = "side must be BUY or SELL, got: " + side;
      return(false);
     }
   bool is_pending = (command_type == "PLACE_PENDING");

   //--- symbol must exist (and joins Market Watch for spec access)
   if(!SymbolSelect(symbol, true))
     {
      code = "SYMBOL_UNKNOWN";
      detail = "SymbolSelect failed for: " + symbol;
      return(false);
     }

   //--- broker trade mode
   long trade_mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   if(trade_mode == SYMBOL_TRADE_MODE_DISABLED ||
      trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY)
     {
      code = "TRADE_MODE_BLOCKED";
      detail = "symbol trade mode is " + QpTradeModeLabel(trade_mode);
      return(false);
     }
   if(trade_mode == SYMBOL_TRADE_MODE_LONGONLY && !is_buy)
     {
      code = "TRADE_MODE_LONGONLY";
      detail = "symbol rejects SELL orders";
      return(false);
     }
   if(trade_mode == SYMBOL_TRADE_MODE_SHORTONLY && is_buy)
     {
      code = "TRADE_MODE_SHORTONLY";
      detail = "symbol rejects BUY orders";
      return(false);
     }

   //--- live quotes
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(bid <= 0.0 || ask <= 0.0)
     {
      code = "NO_QUOTES";
      detail = "no bid/ask for: " + symbol;
      return(false);
     }

   //--- spread cap (MaxSpreadPoints = 0 disables the check)
   if(MaxSpreadPoints > 0)
     {
      long spread = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
      if(spread > MaxSpreadPoints)
        {
         code = "SPREAD_TOO_HIGH";
         detail = "spread " + IntegerToString(spread) + " pts > max " +
                  IntegerToString(MaxSpreadPoints) + " pts";
         return(false);
        }
     }

   //--- volume bounds and step
   double vol_min = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vol_max = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double vol_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(volume < vol_min - 1e-12 || (vol_max > 0.0 && volume > vol_max + 1e-12))
     {
      code = "VOLUME_OUT_OF_RANGE";
      detail = "volume " + DoubleToString(volume, 8) + " outside [" +
               DoubleToString(vol_min, 8) + ", " + DoubleToString(vol_max, 8) + "]";
      return(false);
     }
   if(vol_step > 1e-12)
     {
      double steps = volume / vol_step;
      if(MathAbs(steps - MathRound(steps)) > 1e-4)
        {
         code = "VOLUME_STEP";
         detail = "volume " + DoubleToString(volume, 8) +
                  " is not a multiple of step " + DoubleToString(vol_step, 8);
         return(false);
        }
     }

   //--- order type and reference price
   ENUM_ORDER_TYPE order_type = ORDER_TYPE_BUY;
   double check_price = 0.0;
   if(is_pending)
     {
      if(!QpOrderKindToType(order_kind, order_type))
        {
         code = "BAD_ORDER_KIND";
         detail = "unsupported order_kind: " + order_kind;
         return(false);
        }
      if(price <= 0.0)
        {
         code = "BAD_PRICE";
         detail = "pending command without a trigger price";
         return(false);
        }
      check_price = price;
     }
   else
     {
      order_type = is_buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      check_price = is_buy ? ask : bid;
     }

   //--- SL / TP: side sanity + minimal distance (stops level)
   double dist = (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   double ref = is_pending ? check_price : (is_buy ? bid : ask);
   if(sl != 0.0)
     {
      if(is_buy ? (sl >= ref) : (sl <= ref))
        {
         code = "SL_WRONG_SIDE";
         detail = "stop_loss must sit " + (is_buy ? "below" : "above") +
                  " the " + (is_pending ? "trigger price" : "market");
         return(false);
        }
      if(dist > 0.0 && MathAbs(ref - sl) < dist)
        {
         code = "SL_TOO_CLOSE";
         detail = "|ref - SL| < stops_level " + DoubleToString(dist, 8);
         return(false);
        }
     }
   if(tp != 0.0)
     {
      if(is_buy ? (tp <= ref) : (tp >= ref))
        {
         code = "TP_WRONG_SIDE";
         detail = "take_profit must sit " + (is_buy ? "above" : "below") +
                  " the " + (is_pending ? "trigger price" : "market");
         return(false);
        }
      if(dist > 0.0 && MathAbs(tp - ref) < dist)
        {
         code = "TP_TOO_CLOSE";
         detail = "|TP - ref| < stops_level " + DoubleToString(dist, 8);
         return(false);
        }
     }

   //--- pending trigger price must sit on its correct side of the market
   if(is_pending)
     {
      string why = "";
      switch(order_type)
        {
         case ORDER_TYPE_BUY_LIMIT:
            if(price > ask - dist)
               why = "BUY_LIMIT must be below Ask";
            break;
         case ORDER_TYPE_BUY_STOP:
            if(price < ask + dist)
               why = "BUY_STOP must be above Ask";
            break;
         case ORDER_TYPE_SELL_LIMIT:
            if(price < bid + dist)
               why = "SELL_LIMIT must be above Bid";
            break;
         case ORDER_TYPE_SELL_STOP:
            if(price > bid - dist)
               why = "SELL_STOP must be below Bid";
            break;
        }
      if(why != "")
        {
         code = "PENDING_PRICE_INVALID";
         detail = why;
         return(false);
        }
     }

   //--- trading session
   if(!QpSessionOpen(symbol))
     {
      code = "SESSION_CLOSED";
      detail = "no trading session open for: " + symbol;
      return(false);
     }

   //--- final gate: the terminal's own validation oracle. OrderCheck
   // computes margin/validity exactly as OrderSend would - but it never
   // places anything (slice 2: checks only, never execution).
   MqlTradeRequest request;
   MqlTradeCheckResult check;
   ZeroMemory(request);
   ZeroMemory(check);
   request.action = is_pending ? TRADE_ACTION_PENDING : TRADE_ACTION_DEAL;
   request.symbol = symbol;
   request.volume = volume;
   request.type = order_type;
   request.price = check_price;
   request.sl = sl;
   request.tp = tp;
   request.deviation = deviation > 0 ? (int)deviation : 15;
   request.magic = (int)magic;
   request.comment = "qp-dry-run";
   request.type_time = ORDER_TIME_GTC;
   request.type_filling = ORDER_FILLING_RETURN;
   if(!is_pending)
     {
      // market orders: pick a filling mode the broker actually supports
      long fill_mask = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
      if((fill_mask & SYMBOL_FILLING_FOK) != 0)
         request.type_filling = ORDER_FILLING_FOK;
      else if((fill_mask & SYMBOL_FILLING_IOC) != 0)
         request.type_filling = ORDER_FILLING_IOC;
     }
   if(!OrderCheck(request, check))
     {
      code = "ORDER_CHECK_FAIL";
      detail = "retcode=" + IntegerToString((int)check.retcode) +
               " comment=" + check.comment;
      return(false);
     }

   detail = "OrderCheck ok, required margin " + DoubleToString(check.margin, 2);
   return(true);
  }

//+------------------------------------------------------------------+
//| LIVE execution (slice 3, acceptance C2).                          |
//| Synchronous CTrade, single attempt - no auto-retry: a lost or      |
//| ambiguous attempt is re-issued by the cloud under its own policy,  |
//| and the terminal-level QPCMD_<id> marker turns any redelivery into  |
//| a DUPLICATE ack (acceptance C3: never a 2nd order for one command).|
//| Every ack_message carries broker tickets (order=/deal=) so the     |
//| orders/deals/positions projections can reconcile it (C3).           |
//+------------------------------------------------------------------+
string QpRetcodeClass(const uint retcode)
  {
   switch(retcode)
     {
      case TRADE_RETCODE_REQUOTE:
      case TRADE_RETCODE_PRICE_CHANGED:
      case TRADE_RETCODE_PRICE_OFF:
      case TRADE_RETCODE_TIMEOUT:
      case TRADE_RETCODE_CONNECTION:
      case TRADE_RETCODE_TOO_MANY_REQUESTS:
         return("RETRYABLE");
      case TRADE_RETCODE_DONE:
      case TRADE_RETCODE_DONE_PARTIAL:
      case TRADE_RETCODE_PLACED:
         return("OK");
      default:
         return("FATAL");
     }
  }

bool QpLiveOk(const uint retcode)
  {
   return(retcode == TRADE_RETCODE_DONE ||
          retcode == TRADE_RETCODE_DONE_PARTIAL ||
          retcode == TRADE_RETCODE_PLACED);
  }

// market orders need a filling mode the broker actually supports -
// mirrors the C1 chain's OrderCheck request exactly.
ENUM_ORDER_TYPE_FILLING QpFillingForSymbol(const string symbol, const bool is_pending)
  {
   if(is_pending)
      return(ORDER_FILLING_RETURN);
   long mask = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((mask & SYMBOL_FILLING_FOK) != 0)
      return(ORDER_FILLING_FOK);
   if((mask & SYMBOL_FILLING_IOC) != 0)
      return(ORDER_FILLING_IOC);
   return(ORDER_FILLING_RETURN);
  }

// OPEN_MARKET / PLACE_PENDING: the caller already ran the C1 chain.
void QpExecuteOpenOrPending(const string cmd_json, const string command_id,
                            const string command_type)
  {
   string payload_json;
   if(!QpJsonExtractObject(cmd_json, "payload", payload_json) || payload_json == "")
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL, "PAYLOAD_MISSING: no payload object");
      return;
     }
   string symbol = QpJsonGetString(payload_json, "symbol");
   string side = QpJsonGetString(payload_json, "side");
   double volume = QpJsonGetDouble(payload_json, "volume");
   double sl = QpJsonGetDouble(payload_json, "stop_loss");
   double tp = QpJsonGetDouble(payload_json, "take_profit");
   double price = QpJsonGetDouble(payload_json, "price");
   string order_kind = QpJsonGetString(payload_json, "order_kind");
   long deviation = QpJsonGetLong(payload_json, "max_deviation_points");
   long magic = QpJsonGetLong(payload_json, "magic_number");

   // local fat-finger cap (0 = disabled)
   if(MaxLiveVolume > 0.0 && volume > MaxLiveVolume + 1e-12)
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                       "VOLUME_ABOVE_LOCAL_CAP: volume " + DoubleToString(volume, 8) +
                       " > MaxLiveVolume " + DoubleToString(MaxLiveVolume, 8));
      return;
     }

   bool is_pending = (command_type == "PLACE_PENDING");
   ENUM_ORDER_TYPE order_type = ORDER_TYPE_BUY;
   if(is_pending)
     {
      if(!QpOrderKindToType(order_kind, order_type))
        {
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ, "BAD_ORDER_KIND: " + order_kind);
         return;
        }
     }
   else
      order_type = (side == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   g_trade.SetExpertMagicNumber((ulong)magic);
   g_trade.SetDeviationInPoints((ulong)(deviation > 0 ? deviation : 15));
   g_trade.SetTypeFilling(QpFillingForSymbol(symbol, is_pending));
   g_trade.SetAsyncMode(false);

   bool sent = false;
   if(is_pending)
      sent = g_trade.OrderOpen(symbol, order_type, volume, 0.0, price, sl, tp,
                               ORDER_TIME_GTC, 0, "qp-live");
   else
     {
      double ref = (side == "BUY") ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                                   : SymbolInfoDouble(symbol, SYMBOL_BID);
      sent = g_trade.PositionOpen(symbol, order_type, volume, ref, sl, tp, "qp-live");
     }

   uint retcode = g_trade.ResultRetcode();
   string tickets = "order=" + IntegerToString((long)g_trade.ResultOrder()) +
                    " deal=" + IntegerToString((long)g_trade.ResultDeal());
   if(sent && QpLiveOk(retcode))
     {
      QpLog("INFO", "LIVE " + command_type + " " + command_id + " executed: " + tickets +
            " price=" + DoubleToString(g_trade.ResultPrice(), 8) +
            " retcode=" + IntegerToString((int)retcode));
      QpSendCommandAck(command_id, QP_ACK_EXECUTED,
                       command_type + " executed: " + tickets +
                       " price=" + DoubleToString(g_trade.ResultPrice(), 8) +
                       " volume=" + DoubleToString(g_trade.ResultVolume(), 8) +
                       " retcode=" + IntegerToString((int)retcode),
                       (long)g_trade.ResultOrder(), (long)g_trade.ResultDeal(), 0);
     }
   else
     {
      QpLog("WARN", "LIVE " + command_type + " " + command_id + " failed: " + tickets +
            " retcode=" + IntegerToString((int)retcode) + " " +
            g_trade.ResultRetcodeDescription() + " (" + QpRetcodeClass(retcode) + ")");
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "retcode=" + IntegerToString((int)retcode) + " " +
                       g_trade.ResultRetcodeDescription() +
                       " (" + QpRetcodeClass(retcode) + "): " + tickets);
     }
  }

// MODIFY_POSITION: absent (or zero) sl/tp mean KEEP CURRENT - the command
// can tighten or move protection, it can never remove it (PRD 13.4).
void QpExecuteModifyPosition(const string payload_json, const string command_id)
  {
   if(payload_json == "")
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL, "PAYLOAD_MISSING: no payload object");
      return;
     }
   ulong ticket = (ulong)StringToInteger(QpJsonGetString(payload_json, "position_ticket"));
   if(ticket == 0 || !PositionSelectByTicket(ticket))
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "POSITION_NOT_FOUND: " + QpJsonGetString(payload_json, "position_ticket"));
      return;
     }
   double new_sl = QpJsonGetDouble(payload_json, "sl");  // 0 = absent -> keep
   double new_tp = QpJsonGetDouble(payload_json, "tp"); // 0 = absent -> keep
   double cur_sl = PositionGetDouble(POSITION_SL);
   double cur_tp = PositionGetDouble(POSITION_TP);
   bool is_buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   string symbol = PositionGetString(POSITION_SYMBOL);

   if(new_sl == 0.0)
      new_sl = cur_sl;
   if(new_tp == 0.0)
      new_tp = cur_tp;
   if(new_sl == cur_sl && new_tp == cur_tp)
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                       "NO_CHANGE: effective sl/tp equal the current ones");
      return;
     }

   // side sanity + stops distance against the live market (C1-style)
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double ref = is_buy ? SymbolInfoDouble(symbol, SYMBOL_BID)
                       : SymbolInfoDouble(symbol, SYMBOL_ASK);
   double dist = (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   if(new_sl != 0.0)
     {
      if(is_buy ? (new_sl >= ref) : (new_sl <= ref))
        {
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                          "SL_WRONG_SIDE: stop_loss must sit " +
                          (is_buy ? "below" : "above") + " the market");
         return;
        }
      if(dist > 0.0 && MathAbs(ref - new_sl) < dist)
        {
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                          "SL_TOO_CLOSE: |ref - SL| < stops_level " + DoubleToString(dist, 8));
         return;
        }
     }
   if(new_tp != 0.0)
     {
      if(is_buy ? (new_tp <= ref) : (new_tp >= ref))
        {
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                          "TP_WRONG_SIDE: take_profit must sit " +
                          (is_buy ? "above" : "below") + " the market");
         return;
        }
      if(dist > 0.0 && MathAbs(new_tp - ref) < dist)
        {
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                          "TP_TOO_CLOSE: |TP - ref| < stops_level " + DoubleToString(dist, 8));
         return;
        }
     }

   bool sent = g_trade.PositionModify(ticket, new_sl, new_tp);
   uint retcode = g_trade.ResultRetcode();
   if(sent && QpLiveOk(retcode))
     {
      QpLog("INFO", "LIVE MODIFY_POSITION " + command_id + " executed: position=" +
            IntegerToString((long)ticket) + " sl=" + DoubleToString(new_sl, 8) +
            " tp=" + DoubleToString(new_tp, 8));
      QpSendCommandAck(command_id, QP_ACK_EXECUTED,
                       "MODIFY_POSITION executed: position=" + IntegerToString((long)ticket) +
                       " sl=" + DoubleToString(new_sl, 8) +
                       " tp=" + DoubleToString(new_tp, 8) +
                       " retcode=" + IntegerToString((int)retcode),
                       0, 0, (long)ticket);
     }
   else
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "retcode=" + IntegerToString((int)retcode) + " " +
                       g_trade.ResultRetcodeDescription() +
                       " (" + QpRetcodeClass(retcode) + "): position=" +
                       IntegerToString((long)ticket));
     }
  }

// CLOSE_POSITION: absent volume = full close; present volume = partial.
void QpExecuteClosePosition(const string payload_json, const string command_id)
  {
   if(payload_json == "")
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL, "PAYLOAD_MISSING: no payload object");
      return;
     }
   ulong ticket = (ulong)StringToInteger(QpJsonGetString(payload_json, "position_ticket"));
   double volume = QpJsonGetDouble(payload_json, "volume"); // 0 = full close
   if(ticket == 0 || !PositionSelectByTicket(ticket))
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "POSITION_NOT_FOUND: " + QpJsonGetString(payload_json, "position_ticket"));
      return;
     }
   string symbol = PositionGetString(POSITION_SYMBOL);
   double pos_volume = PositionGetDouble(POSITION_VOLUME);

   g_trade.SetDeviationInPoints(15);
   g_trade.SetAsyncMode(false);

   bool sent = false;
   bool partial = false;
   if(volume <= 0.0 || volume >= pos_volume - 1e-12)
      sent = g_trade.PositionClose(ticket);
   else
     {
      double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      if(step > 1e-12 && MathAbs(volume / step - MathRound(volume / step)) > 1e-4)
        {
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ,
                          "VOLUME_STEP: close volume " + DoubleToString(volume, 8) +
                          " is not a multiple of step " + DoubleToString(step, 8));
         return;
        }
      partial = true;
      sent = g_trade.PositionClosePartial(ticket, volume);
     }

   uint retcode = g_trade.ResultRetcode();
   if(sent && QpLiveOk(retcode))
     {
      double closed = partial ? volume : pos_volume;
      double remaining = partial ? (pos_volume - volume) : 0.0;
      QpLog("INFO", "LIVE CLOSE_POSITION " + command_id + " executed (" +
            (partial ? "partial" : "full") + "): position=" + IntegerToString((long)ticket));
      QpSendCommandAck(command_id, QP_ACK_EXECUTED,
                       "CLOSE_POSITION executed (" + (partial ? "partial" : "full") +
                       "): position=" + IntegerToString((long)ticket) +
                       " closed=" + DoubleToString(closed, 8) +
                       " remaining=" + DoubleToString(remaining, 8) +
                       " deal=" + IntegerToString((long)g_trade.ResultDeal()) +
                       " retcode=" + IntegerToString((int)retcode),
                       0, (long)g_trade.ResultDeal(), (long)ticket);
     }
   else
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "retcode=" + IntegerToString((int)retcode) + " " +
                       g_trade.ResultRetcodeDescription() +
                       " (" + QpRetcodeClass(retcode) + "): position=" +
                       IntegerToString((long)ticket));
     }
  }

// CANCEL_ORDER: delete a live pending order by ticket.
void QpExecuteCancelOrder(const string payload_json, const string command_id)
  {
   if(payload_json == "")
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL, "PAYLOAD_MISSING: no payload object");
      return;
     }
   ulong ticket = (ulong)StringToInteger(QpJsonGetString(payload_json, "order_ticket"));
   if(ticket == 0 || !OrderSelect(ticket))
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "ORDER_NOT_FOUND: " + QpJsonGetString(payload_json, "order_ticket"));
      return;
     }
   bool sent = g_trade.OrderDelete(ticket);
   uint retcode = g_trade.ResultRetcode();
   if(sent && QpLiveOk(retcode))
     {
      QpLog("INFO", "LIVE CANCEL_ORDER " + command_id + " executed: order=" +
            IntegerToString((long)ticket));
      QpSendCommandAck(command_id, QP_ACK_EXECUTED,
                       "CANCEL_ORDER executed: order=" + IntegerToString((long)ticket) +
                       " retcode=" + IntegerToString((int)retcode),
                       (long)ticket, 0, 0);
     }
   else
     {
      QpSendCommandAck(command_id, QP_ACK_EXECUTE_FAIL,
                       "retcode=" + IntegerToString((int)retcode) + " " +
                       g_trade.ResultRetcodeDescription() +
                       " (" + QpRetcodeClass(retcode) + "): order=" +
                       IntegerToString((long)ticket));
     }
  }

// LIVE dispatch (acceptance C2). Hard guards first, then per-type paths.
void QpExecuteCommand(const string cmd_json, const string command_id,
                      const string command_type)
  {
   // hard guard: live execution ONLY on DEMO accounts, whatever the inputs
   // say - a REAL/CONTEST account never sees an OrderSend from this EA
   long acc_mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(acc_mode != ACCOUNT_TRADE_MODE_DEMO)
     {
      QpLog("ERROR", "LIVE " + command_type + " " + command_id +
            " refused: account mode is " + QpAccountModeLabel(acc_mode) + ", not DEMO");
      QpSendCommandAck(command_id, QP_ACK_NOT_DEMO,
                       "LiveTrading requires a DEMO account; account mode is " +
                       QpAccountModeLabel(acc_mode));
      return;
     }

   // risk-increasing types: the SAME C1 chain gates live execution
   if(QpIsRiskIncreasing(command_type))
     {
      string code = "", detail = "";
      if(!QpPreTradeChecks(cmd_json, command_type, code, detail))
        {
         QpLog("WARN", "LIVE " + command_type + " " + command_id +
               " rejected by pre-trade checks: " + code + " - " + detail);
         QpSendCommandAck(command_id, QP_ACK_EXECUTE_REJ, code + ": " + detail);
         return;
        }
      QpExecuteOpenOrPending(cmd_json, command_id, command_type);
      return;
     }

   string payload_json;
   bool has_payload = QpJsonExtractObject(cmd_json, "payload", payload_json);

   if(command_type == "MODIFY_POSITION")
      QpExecuteModifyPosition(has_payload ? payload_json : "", command_id);
   else if(command_type == "CLOSE_POSITION")
      QpExecuteClosePosition(has_payload ? payload_json : "", command_id);
   else if(command_type == "CANCEL_ORDER")
      QpExecuteCancelOrder(has_payload ? payload_json : "", command_id);
   else
     {
      // STOP_NEW_RISK / RECONCILE are cloud-side policies with no terminal
      // trade action - receipt-only, as in every mode
      QpSendCommandAck(command_id, QP_ACK_OBSERVE_ONLY,
                       "cloud-side policy command; no terminal action (" + command_type + ")");
     }
  }

void QpHandleCommand(const string cmd_json)
  {
   string command_id   = QpJsonGetString(cmd_json, "command_id");
   string command_type = QpJsonGetString(cmd_json, "command_type");
   long   expires_unix = QpJsonGetLong(cmd_json, "expires_at_unix");

   if(command_id == "")
     {
      QpLog("ERROR", "command without command_id; ignored");
      return;
     }

   // duplicate delivery (at-least-once transport): recover a journal that
   // still owes an ack FIRST, else ack DUPLICATE - never any side effects
   if(QpCommandProcessed(command_id) || QpCommandStateExists(command_id))
     {
      if(QpCommandStateRecover(command_id))
         return; // the journal just re-sent its verdict
      QpLog("DEBUG", "command " + command_id + " duplicate; acking DUPLICATE");
      QpSendCommandAck(command_id, QP_ACK_DUPLICATE, "already processed (terminal dedup)");
      return;
     }

   // crash-safe journal (R1): a live command that mutates broker state is
   // journaled EXECUTING on disk BEFORE the processed mark and the CTrade
   // call - a crash anywhere past this point leaves a recoverable journal
   if(!DryRunMode && LiveTrading && QpCommandMutatesBrokerState(command_type))
      QpCommandStateInit(command_id, command_type);

   // mark FIRST, ack second: if this ack is lost in transit, the next poll's
   // recently_acked echo still lets the cloud reconcile the UNKNOWN row
   QpMarkCommandProcessed(command_id);
   QpRememberCommand(command_id);

   if(expires_unix > 0 && TimeGMT() > (datetime)expires_unix)
     {
      QpLog("INFO", "command " + command_id + " (" + command_type +
            ") received after expiry; acking EXPIRED");
      QpSendCommandAck(command_id, QP_ACK_EXPIRED, "received after expires_at");
      return;
     }

   // slice 2 DRY_RUN (PRD 13.5 / C1): risk-increasing commands run the
   // full pre-trade check chain - still NO order is ever sent
   if(DryRunMode && QpIsRiskIncreasing(command_type))
     {
      string code = "", detail = "";
      if(QpPreTradeChecks(cmd_json, command_type, code, detail))
        {
         QpLog("INFO", "DRY_RUN " + command_type + " " + command_id +
               ": pre-trade checks passed (" + detail + ")");
         QpSendCommandAck(command_id, QP_ACK_DRY_RUN_PASS,
                          "pre-trade checks passed (" + command_type + "): " + detail);
        }
      else
        {
         QpLog("WARN", "DRY_RUN " + command_type + " " + command_id +
               " rejected: " + code + " - " + detail);
         QpSendCommandAck(command_id, QP_ACK_DRY_RUN_REJECT, code + ": " + detail);
        }
      return;
     }

   // slice 3 LIVE (acceptance C2): DryRunMode=false AND LiveTrading=true;
   // the DEMO-account hard guard lives inside QpExecuteCommand. The
   // DryRunMode branch above short-circuits first, so DryRunMode=true can
   // NEVER reach this path.
   if(!DryRunMode && LiveTrading)
     {
      QpExecuteCommand(cmd_json, command_id, command_type);
      return;
     }

   // OBSERVE_ONLY (PRD 21.3): the payload is logged, NEVER executed
   QpLog("INFO", "command " + command_id + " (" + command_type +
         ") acked OBSERVE_ONLY_ACK (receipt only, no execution)");
   QpSendCommandAck(command_id, QP_ACK_OBSERVE_ONLY,
                    "received; not executed (OBSERVE_ONLY)");
  }

//+------------------------------------------------------------------+
//| Command poll: identity + recently_acked -> agent -> command list |
//| PRD 2.5: the cloud returns at most one QUEUED command per poll. |
//+------------------------------------------------------------------+
bool QpPollCommands()
  {
   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", g_terminal_json);
   o.AddRaw("account", QpAccountJson());
   o.AddRaw("recently_acked", QpRecentCommandsJson());
   o.AddStr("observed_at", QpIsoNow());
   string body = "";
   if(!QpHttpPost("/ea/v1/commands/poll", o.Dump(), body))
      return(false);

   string array_json;
   if(!QpJsonExtractArray(body, "commands", array_json))
      return(true); // no commands key / empty array -> nothing pending

   string objs[];
   int n = QpJsonSplitObjects(array_json, objs);
   for(int i = 0; i < n; i++)
      QpHandleCommand(objs[i]);
   return(true);
  }

//+------------------------------------------------------------------+
//| Economic calendar (MT5 built-in, read-only)                       |
//+------------------------------------------------------------------+
string QpCalendarImportanceLabel(const long importance)
  {
   switch((int)importance)
     {
      case CALENDAR_IMPORTANCE_NONE:     return("NONE");
      case CALENDAR_IMPORTANCE_LOW:      return("LOW");
      case CALENDAR_IMPORTANCE_MODERATE: return("MODERATE");
      case CALENDAR_IMPORTANCE_HIGH:     return("HIGH");
     }
   return("UNKNOWN_" + IntegerToString(importance));
  }

string QpCalendarMultiplierLabel(const long multiplier)
  {
   switch((int)multiplier)
     {
      case CALENDAR_MULTIPLIER_NONE:      return("NONE");
      case CALENDAR_MULTIPLIER_THOUSANDS: return("THOUSANDS");
      case CALENDAR_MULTIPLIER_MILLIONS:  return("MILLIONS");
      case CALENDAR_MULTIPLIER_BILLIONS:  return("BILLIONS");
      case CALENDAR_MULTIPLIER_TRILLIONS: return("TRILLIONS");
     }
   return("UNKNOWN_" + IntegerToString(multiplier));
  }

// Terminal marks unset calendar values with extreme long sentinels
// (|LONG_MIN/LONG_MAX| ~ 9.2e18); real figures stay far below this bound.
bool QpCalendarValueSane(const long value)
  {
   return(value <= 1000000000000000 && value >= -1000000000000000);
  }

bool QpSendCalendar()
  {
   // Calendar times follow the trade-server clock, but the pipeline (PG
   // storage, API windows, dashboard countdown) speaks UTC. Convert via
   // the live offset TimeTradeServer - TimeGMT (DST-safe at report time;
   // MQL5 exposes no server-timezone API, doc 21.3 no invention).
   long utc_offset = (long)TimeTradeServer() - (long)TimeGMT();

   // the CalendarValueHistory query window itself stays on the
   // trade-server clock - that is the axis the calendar API speaks
   datetime now  = TimeTradeServer();
   datetime from = now - QP_CALENDAR_PAST_SEC;
   datetime to   = now + (datetime)CalendarWindowHours * 3600;

   MqlCalendarValue values[];
   ResetLastError();
   if(!CalendarValueHistory(values, from, to))
     {
      // unavailable calendar (broker/terminal) is a data condition, not a
      // transport failure - nothing to report, refresh retries later
      QpLog("INFO", "calendar unavailable (broker/terminal), err=" +
            IntegerToString(GetLastError()));
      return(true);
     }

   int total = ArraySize(values);
   int batch_total = (total + QP_CALENDAR_PER_BATCH - 1) / QP_CALENDAR_PER_BATCH;
   if(batch_total == 0)
      batch_total = 1;

   int sent = 0;
   for(int batch_index = 0; batch_index < batch_total; batch_index++)
     {
      string events_json = "";
      int in_batch = 0;
      for(int i = batch_index * QP_CALENDAR_PER_BATCH;
          i < (batch_index + 1) * QP_CALENDAR_PER_BATCH && i < total; i++)
        {
         MqlCalendarEvent ev;
         if(!CalendarEventById(values[i].event_id, ev))
            continue;
         MqlCalendarCountry country;
         if(!CalendarCountryById(ev.country_id, country))
            continue;

         QpJsonObject e;
         e.Clear();
         e.AddStr("event_uid", IntegerToString((long)values[i].id));
         e.AddStr("country_code", country.code);
         e.AddStr("currency", country.currency);
         e.AddStr("name", ev.name);
         e.AddStr("importance", QpCalendarImportanceLabel(ev.importance));
         e.AddStr("multiplier", QpCalendarMultiplierLabel(ev.multiplier));
         e.AddInt("digits", ev.digits);
         e.AddStr("event_time", QpIsoTime((datetime)((long)values[i].time - utc_offset)));
         if(QpCalendarValueSane(values[i].actual_value))
            e.AddNumStr("actual", IntegerToString(values[i].actual_value));
         if(QpCalendarValueSane(values[i].forecast_value))
            e.AddNumStr("forecast", IntegerToString(values[i].forecast_value));
         if(QpCalendarValueSane(values[i].prev_value))
            e.AddNumStr("previous", IntegerToString(values[i].prev_value));
         if(in_batch > 0)
            events_json += ",";
         events_json += e.Dump();
         in_batch++;
        }
      if(in_batch == 0)
         continue;

      QpJsonObject o;
      o.Clear();
      o.AddStr("ea_version", QP_EA_VERSION);
      o.AddRaw("terminal", g_terminal_json);
      o.AddRaw("account", QpAccountJson());
      o.AddInt("batch_index", batch_index);
      o.AddInt("batch_total", batch_total);
      o.AddStr("window_from", QpIsoTime((datetime)((long)from - utc_offset)));
      o.AddStr("window_to", QpIsoTime((datetime)((long)to - utc_offset)));
      o.AddRaw("events", "[" + events_json + "]");
      o.AddStr("observed_at", QpIsoNow());
      if(!QpHttpPost("/ea/v1/calendar", o.Dump()))
         return(false);
      sent += in_batch;
     }
   QpLog("INFO", "reported " + IntegerToString(sent) + " calendar event(s) in " +
         IntegerToString(batch_total) + " batch(es)");
   return(true);
  }

//+------------------------------------------------------------------+
//| Symbol specs (PRD 5.4) in batches                                |
//+------------------------------------------------------------------+
bool QpSendSymbolSpecs()
  {
   int total = SymbolsTotal(false); // Market Watch scope (PRD 10.4)
   if(MaxSymbols > 0 && total > MaxSymbols)
      total = MaxSymbols;

   int batch_total = (total + QP_SYMBOLS_PER_BATCH - 1) / QP_SYMBOLS_PER_BATCH;
   if(batch_total == 0)
      batch_total = 1;

   int sent = 0;
   for(int batch_index = 0; batch_index < batch_total; batch_index++)
     {
      string symbols_json = "";
      int in_batch = 0;
      for(int i = batch_index * QP_SYMBOLS_PER_BATCH;
          i < (batch_index + 1) * QP_SYMBOLS_PER_BATCH && i < total; i++)
        {
         string name = SymbolName(i, false);
         if(name == "")
            continue;
         QpJsonObject s;
         s.Clear();
         s.AddStr("broker_symbol", name);
         s.AddBool("visible", SymbolInfoInteger(name, SYMBOL_VISIBLE) != 0);
         // SYMBOL_SELECT: the symbol is selected in Market Watch (build has no SYMBOL_SELECTABLE)
         s.AddBool("selected", SymbolInfoInteger(name, SYMBOL_SELECT) != 0);
         s.AddStr("trade_mode", QpTradeModeLabel(SymbolInfoInteger(name, SYMBOL_TRADE_MODE)));
         s.AddInt("trade_mode_raw", SymbolInfoInteger(name, SYMBOL_TRADE_MODE));
         s.AddRaw("filling_modes", QpFillingModes(SymbolInfoInteger(name, SYMBOL_FILLING_MODE)));
         // honest label: value is broker-dependent, raw int is authoritative (PRD 21.3)
         s.AddStr("calc_mode", "CALC_MODE_" + IntegerToString(SymbolInfoInteger(name, SYMBOL_TRADE_CALC_MODE)));
         s.AddInt("digits", SymbolInfoInteger(name, SYMBOL_DIGITS));
         s.AddNumStr("point", DoubleToString(SymbolInfoDouble(name, SYMBOL_POINT), 8));
         s.AddNumStr("volume_min", DoubleToString(SymbolInfoDouble(name, SYMBOL_VOLUME_MIN), 8));
         s.AddNumStr("volume_max", DoubleToString(SymbolInfoDouble(name, SYMBOL_VOLUME_MAX), 8));
         s.AddNumStr("volume_step", DoubleToString(SymbolInfoDouble(name, SYMBOL_VOLUME_STEP), 8));
         s.AddNumStr("tick_size", DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_SIZE), 8));
         s.AddNumStr("tick_value_profit", DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE_PROFIT), 8));
         s.AddNumStr("tick_value_loss", DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE_LOSS), 8));
         s.AddInt("stops_level", SymbolInfoInteger(name, SYMBOL_TRADE_STOPS_LEVEL));
         s.AddInt("freeze_level", SymbolInfoInteger(name, SYMBOL_TRADE_FREEZE_LEVEL));
         s.AddNumStr("contract_size", DoubleToString(SymbolInfoDouble(name, SYMBOL_TRADE_CONTRACT_SIZE), 8));
         s.AddStr("currency_base", SymbolInfoString(name, SYMBOL_CURRENCY_BASE));
         s.AddStr("currency_profit", SymbolInfoString(name, SYMBOL_CURRENCY_PROFIT));
         s.AddStr("currency_margin", SymbolInfoString(name, SYMBOL_CURRENCY_MARGIN));
         s.AddInt("spread", SymbolInfoInteger(name, SYMBOL_SPREAD));
         int digits = (int)SymbolInfoInteger(name, SYMBOL_DIGITS);
         s.AddNumStr("bid", DoubleToString(SymbolInfoDouble(name, SYMBOL_BID), digits));
         s.AddNumStr("ask", DoubleToString(SymbolInfoDouble(name, SYMBOL_ASK), digits));
         if(in_batch > 0)
            symbols_json += ",";
         symbols_json += s.Dump();
         in_batch++;
        }
      if(in_batch == 0)
         continue;

      QpJsonObject o;
      o.Clear();
      o.AddStr("ea_version", QP_EA_VERSION);
      o.AddRaw("terminal", g_terminal_json);
      o.AddRaw("account", QpAccountJson());
      o.AddInt("batch_index", batch_index);
      o.AddInt("batch_total", batch_total);
      o.AddRaw("symbols", "[" + symbols_json + "]");
      o.AddStr("observed_at", QpIsoNow());
      if(!QpHttpPost("/ea/v1/symbols", o.Dump()))
         return(false);
      sent += in_batch;
     }
   QpLog("INFO", "reported specs for " + IntegerToString(sent) + " symbols in " +
         IntegerToString(batch_total) + " batch(es)");
   return(true);
  }

//+------------------------------------------------------------------+
//| Shutdown note                                                    |
//+------------------------------------------------------------------+
void QpSendShutdown(const string reason)
  {
   QpJsonObject o;
   o.Clear();
   o.AddStr("ea_version", QP_EA_VERSION);
   o.AddRaw("terminal", QpTerminalJson());
   o.AddRaw("account", QpAccountJson());
   o.AddStr("reason", reason);
   o.AddStr("observed_at", QpIsoNow());
   if(!QpHttpPost("/ea/v1/shutdown", o.Dump()))
      QpLog("ERROR", "shutdown note not delivered (agent unavailable?)");
  }

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   QpLog("INFO", "QuantPilotBridge " + QP_EA_VERSION +
         " starting (telemetry; DryRunMode=" + (DryRunMode ? "true" : "false") +
         "; LiveTrading=" + (LiveTrading ? "true" : "false") + ")");

   // --- safety gates before any network activity ---
   if(AgentToken == "")
     {
      QpLog("ERROR", "AgentToken input is empty; refusing to start");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(StringFind(AgentEndpoint, "http://127.0.0.1") != 0 &&
      StringFind(AgentEndpoint, "http://localhost") != 0)
     {
      QpLog("ERROR", "AgentEndpoint must be loopback (http://127.0.0.1 or http://localhost), got: "
            + AgentEndpoint);
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(HeartbeatSeconds < 5 || HeartbeatSeconds > 15)
     {
      QpLog("ERROR", "HeartbeatSeconds must be within 5..15 (PRD 5.7)");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(CommandPollSeconds < 1 || CommandPollSeconds > 120)
     {
      QpLog("ERROR", "CommandPollSeconds must be within 1..120");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(MaxSpreadPoints < 0)
     {
      QpLog("ERROR", "MaxSpreadPoints must be >= 0");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(MaxLiveVolume < 0.0)
     {
      QpLog("ERROR", "MaxLiveVolume must be >= 0 (0 disables the local cap)");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(LiveTrading && DryRunMode)
      QpLog("WARN", "DryRunMode=true takes precedence: NO real orders will be sent");
   if(LiveTrading)
     {
      long init_mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
      if(init_mode == ACCOUNT_TRADE_MODE_DEMO)
         QpLog("INFO", "LiveTrading armed on a DEMO account: real CTrade execution enabled (C2)");
      else
         QpLog("ERROR", "LiveTrading=true on a " + QpAccountModeLabel(init_mode) +
               " account: every live command will be refused (LIVE_REFUSED_NOT_DEMO)");
     }
   if(BarHistoryCount < 50 || BarHistoryCount > 2000)
     {
      QpLog("ERROR", "BarHistoryCount must be within 50..2000");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(!QpInitBarTimeframes())
      return(INIT_PARAMETERS_INCORRECT);

   // synchronous CTrade, quiet journal; the live path configures the rest
   // (magic/deviation/filling) per command
   g_trade.LogLevel(LOG_LEVEL_ERRORS);
   g_trade.SetAsyncMode(false);

   // R1: recover journals from a previous run (owed acks re-sent, crashed
   // executions flagged EXECUTE_UNRESOLVED) BEFORE polling for new commands
   QpCommandStateScan();

   EventSetTimer(HeartbeatSeconds);

   // first registration attempt; agent may not be up yet - OnTimer retries
   if(QpSendRegister())
     {
      g_registered = true;
      g_last_deal_time = 0; // new account context -> full history backfill
      if(QpSendSymbolSpecs())
         g_last_specs_at = TimeGMT();
      if(QpSendCalendar())
         g_last_calendar_at = TimeGMT();
      QpSendBars(); // initial closed-bar backfill; failures retry on timer
     }
   else
      QpLog("INFO", "initial register failed; will retry on timer");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   string reason_str = "reason=" + IntegerToString(reason);
   if(g_registered)
      QpSendShutdown(reason_str);
   else
      QpLog("INFO", "deinit without prior registration: " + reason_str);
  }

//+------------------------------------------------------------------+
//| Timer: heartbeat + spec refresh + register retry                |
//+------------------------------------------------------------------+
void OnTimer()
  {
   // identity change (user switched account in the terminal) -> re-register (PRD 6.3)
   if(g_registered && QpIdentityChanged())
     {
      QpLog("INFO", "account identity changed; re-registering");
      g_registered = false;
     }

   if(!g_registered)
     {
      if(QpSendRegister())
        {
         g_registered = true;
         g_last_deal_time = 0; // new account context -> full history backfill
         QpResetBarBackfill();
         if(QpSendSymbolSpecs())
            g_last_specs_at = TimeGMT();
         if(QpSendCalendar())
            g_last_calendar_at = TimeGMT();
        }
      return;
     }

   if(g_last_specs_at == 0 || TimeGMT() - g_last_specs_at >= QP_SPEC_REFRESH_SEC)
     {
      if(QpSendSymbolSpecs())
         g_last_specs_at = TimeGMT();
     }

   QpSendHeartbeat();

   // position snapshot every heartbeat tick (read-only reconcile, doc 5.2)
   QpSendPositions();

   // pending-order snapshot every heartbeat tick (state sync, doc 12.2)
   QpSendOrders();

   // incremental history deals (read-only; new account backfills fully)
   if(g_last_deals_at == 0 || TimeGMT() - g_last_deals_at >= QP_DEALS_REFRESH_SEC)
     {
      if(QpSendDeals())
         g_last_deals_at = TimeGMT();
     }

   // closed bars per configured timeframe (strategy-engine input,
   // read-only; only new bars are sent, cloud upsert is idempotent - PRD 7)
   QpSendBars();

   // cloud config poll (informational only, never applied - PRD 21.3)
   if(g_last_config_at == 0 || TimeGMT() - g_last_config_at >= QP_CONFIG_REFRESH_SEC)
     {
      if(QpSendConfigPoll())
         g_last_config_at = TimeGMT();
     }

   // command channel poll (OBSERVE_ONLY / DRY_RUN / LIVE(demo) per inputs - PRD 21.3)
   if(g_last_command_at == 0 || TimeGMT() - g_last_command_at >= CommandPollSeconds)
     {
      if(QpPollCommands())
         g_last_command_at = TimeGMT();
     }

   // R1: periodic journal sweep - re-send acks that failed while the local
   // agent was down, throttled to one sweep per minute
   if(g_last_cmdstate_scan == 0 || TimeGMT() - g_last_cmdstate_scan >= 60)
     {
      QpCommandStateScan();
      g_last_cmdstate_scan = TimeGMT();
     }

   // economic calendar window (read-only; actual values arrive after release)
   if(g_last_calendar_at == 0 || TimeGMT() - g_last_calendar_at >= QP_CALENDAR_REFRESH_SEC)
     {
      if(QpSendCalendar())
         g_last_calendar_at = TimeGMT();
     }
  }
//+------------------------------------------------------------------+