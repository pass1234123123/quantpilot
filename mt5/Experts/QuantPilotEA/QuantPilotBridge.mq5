//+------------------------------------------------------------------+
//| QuantPilotBridge.mq5                                             |
//| QuantPilot AI - MT5 EA bridge, SLICE 1: OBSERVE_ONLY telemetry.   |
//|                                                                   |
//| What this EA does (and ALL it does in this slice):               |
//|   - reports terminal/account identity to the local agent          |
//|   - reports symbol trading specifications (PRD 5.4)                |
//|   - sends doc A.3 account heartbeats on a timer                  |
//|   - sends a shutdown note on removal                             |
//|                                                                   |
//| What it MUST NOT contain (PRD 21.3):                             |
//|   - no OrderSend / OrderCheck / CTrade / any order execution      |
//|   - no position modification, no risk limits, no trading logic    |
//|   - it never acts on responses other than logging them            |
//|                                                                   |
//| Identity verification (PRD 6.3): every payload carries the same   |
//| terminal + account identity; the cloud flags IDENTITY_MISMATCH.   |
//| Account switching inside the terminal re-triggers registration.   |
//+------------------------------------------------------------------+
#property copyright "QuantPilot AI"
#property version   "1.00"
#property strict

#include <QuantPilot\QpJson.mqh>

#define QP_EA_VERSION        "1.0.0"
#define QP_HTTP_TIMEOUT_MS   5000
#define QP_SYMBOLS_PER_BATCH 25
#define QP_SPEC_REFRESH_SEC  3600   // re-report full specs hourly

//--- inputs (PRD 5.7 subset relevant to this slice)
input string AgentEndpoint    = "http://127.0.0.1:8787"; // Local agent base URL (loopback only)
input string AgentToken       = "";                     // Shared local token (X-QP-Token)
input int    HeartbeatSeconds = 10;                     // Heartbeat interval, 5..15s (PRD 5.7)
input int    MaxSymbols       = 100;                    // Max Market Watch symbols to report
input string LogLevel         = "INFO";                 // DEBUG | INFO | ERROR

//--- runtime state
bool     g_registered      = false;
string   g_last_login      = "";
string   g_last_server     = "";
string   g_terminal_json    = "";
datetime g_last_specs_at   = 0;

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
   o.AddStr("observed_at", QpIsoNow());
   return(QpHttpPost("/ea/v1/heartbeat", o.Dump()));
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
   QpLog("INFO", "QuantPilotBridge " + QP_EA_VERSION + " starting (OBSERVE_ONLY telemetry)");

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

   EventSetTimer(HeartbeatSeconds);

   // first registration attempt; agent may not be up yet - OnTimer retries
   if(QpSendRegister())
     {
      g_registered = true;
      if(QpSendSymbolSpecs())
         g_last_specs_at = TimeGMT();
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
         if(QpSendSymbolSpecs())
            g_last_specs_at = TimeGMT();
        }
      return;
     }

   if(g_last_specs_at == 0 || TimeGMT() - g_last_specs_at >= QP_SPEC_REFRESH_SEC)
     {
      if(QpSendSymbolSpecs())
         g_last_specs_at = TimeGMT();
     }

   QpSendHeartbeat();
  }
//+------------------------------------------------------------------+