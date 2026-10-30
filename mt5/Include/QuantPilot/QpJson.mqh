//+------------------------------------------------------------------+
//| QpJson.mqh - minimal JSON building helpers for QuantPilot EA     |
//| Hand-rolled serializer: no parsing, only construction. Money and |
//| volume values are serialized as decimal STRINGS (PRD 13.1).      |
//+------------------------------------------------------------------+
#property copyright "QuantPilot AI"
#property strict

//--- escape a string for JSON output (quotes, backslash, control chars)
string QpJsonEscape(const string value)
  {
   string out = "";
   int    len = StringLen(value);
   for(int i = 0; i < len; i++)
     {
      ushort c = StringGetCharacter(value, i);
      if(c == '"')
         out += "\\\"";
      else if(c == '\\')
         out += "\\\\";
      else if(c == '\n')
         out += "\\n";
      else if(c == '\r')
         out += "\\r";
      else if(c == '\t')
         out += "\\t";
      else if(c < 32)
         out += StringFormat("\\u%04x", c);
      else
         out += StringSubstr(value, i, 1);
     }
   return(out);
  }

//--- tiny ordered key-value JSON object builder
class QpJsonObject
  {
private:
   string m_parts[];
   int    m_count;

public:
   void Clear() { m_count = 0; ArrayResize(m_parts, 0); }

   void AddStr(const string key, const string value)
     {
      _push(_key(key) + "\"" + QpJsonEscape(value) + "\"");
     }

   // numeric value already formatted as exact decimal string
   void AddNumStr(const string key, const string decimal_string)
     {
      _push(_key(key) + "\"" + decimal_string + "\"");
     }

   void AddInt(const string key, const long value)
     {
      _push(_key(key) + IntegerToString(value));
     }

   void AddBool(const string key, const bool value)
     {
      _push(_key(key) + (value ? "true" : "false"));
     }

   // pre-built JSON fragment (object/array) is inserted verbatim
   void AddRaw(const string key, const string raw_json)
     {
      _push(_key(key) + raw_json);
     }

   string Dump()
     {
      string body = "";
      for(int i = 0; i < m_count; i++)
        {
         if(i > 0)
            body += ",";
         body += m_parts[i];
        }
      return("{" + body + "}");
     }

private:
   string _key(const string key) { return("\"" + QpJsonEscape(key) + "\":"); }

   void _push(const string part)
     {
      int n = ArraySize(m_parts);
      ArrayResize(m_parts, n + 1);
      m_parts[n] = part;
      m_count = n + 1;
     }
  };
//+------------------------------------------------------------------+