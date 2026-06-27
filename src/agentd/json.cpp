#include "json.h"

#include <cctype>
#include <cstdio>
#include <sstream>

namespace agentd::json {

namespace {

void encode_str(std::ostringstream &o, const std::string &s) {
    o << '"';
    for (char c : s) {
        switch (c) {
        case '"':  o << "\\\""; break;
        case '\\': o << "\\\\"; break;
        case '\n': o << "\\n";  break;
        case '\r': o << "\\r";  break;
        case '\t': o << "\\t";  break;
        default:
            if (static_cast<unsigned char>(c) < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                o << buf;
            } else {
                o << c;
            }
        }
    }
    o << '"';
}

void encode_v(std::ostringstream &o, const Value &v) {
    if (v.is_null())      { o << "null"; }
    else if (v.is_bool()) { o << (v.as_bool() ? "true" : "false"); }
    else if (v.is_int())  { o << v.as_int(); }
    else if (v.is_str())  { encode_str(o, v.as_str()); }
    else if (v.is_arr()) {
        o << '[';
        bool first = true;
        for (auto &el : v.as_arr()) {
            if (!first) o << ',';
            first = false;
            encode_v(o, el);
        }
        o << ']';
    }
    else if (v.is_obj()) {
        o << '{';
        bool first = true;
        for (auto &kv : v.as_obj()) {
            if (!first) o << ',';
            first = false;
            encode_str(o, kv.first);
            o << ':';
            encode_v(o, kv.second);
        }
        o << '}';
    }
}

struct Parser {
    const std::string &s;
    size_t i = 0;
    std::string err;

    Parser(const std::string &str) : s(str) {}

    void skip_ws() { while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i]))) ++i; }
    bool eof() const { return i >= s.size(); }
    bool fail(const char *m) { err = m; return false; }

    bool parse(Value &out) {
        skip_ws();
        if (eof()) return fail("unexpected eof");
        char c = s[i];
        if (c == '{') return parse_obj(out);
        if (c == '[') return parse_arr(out);
        if (c == '"') return parse_str(out);
        if (c == '-' || std::isdigit(static_cast<unsigned char>(c))) return parse_num(out);
        if (c == 't' || c == 'f') return parse_bool(out);
        if (c == 'n') return parse_null(out);
        return fail("unexpected char");
    }

    bool parse_obj(Value &out) {
        Object obj;
        ++i; /* { */
        skip_ws();
        if (!eof() && s[i] == '}') { ++i; out = Value(std::move(obj)); return true; }
        while (true) {
            skip_ws();
            if (eof() || s[i] != '"') return fail("expected string key");
            Value k;
            if (!parse_str(k)) return false;
            skip_ws();
            if (eof() || s[i] != ':') return fail("expected ':'");
            ++i;
            Value v;
            if (!parse(v)) return false;
            obj.emplace(k.as_str(), std::move(v));
            skip_ws();
            if (eof()) return fail("unexpected eof in object");
            if (s[i] == ',') { ++i; continue; }
            if (s[i] == '}') { ++i; out = Value(std::move(obj)); return true; }
            return fail("expected ',' or '}'");
        }
    }

    bool parse_arr(Value &out) {
        Array arr;
        ++i; /* [ */
        skip_ws();
        if (!eof() && s[i] == ']') { ++i; out = Value(std::move(arr)); return true; }
        while (true) {
            Value v;
            if (!parse(v)) return false;
            arr.push_back(std::move(v));
            skip_ws();
            if (eof()) return fail("unexpected eof in array");
            if (s[i] == ',') { ++i; continue; }
            if (s[i] == ']') { ++i; out = Value(std::move(arr)); return true; }
            return fail("expected ',' or ']'");
        }
    }

    bool parse_str(Value &out) {
        ++i; /* " */
        std::string str;
        while (!eof()) {
            char c = s[i++];
            if (c == '"') { out = Value(std::move(str)); return true; }
            if (c == '\\') {
                if (eof()) return fail("bad escape");
                char e = s[i++];
                switch (e) {
                case '"':  str += '"'; break;
                case '\\': str += '\\'; break;
                case '/':  str += '/'; break;
                case 'n':  str += '\n'; break;
                case 'r':  str += '\r'; break;
                case 't':  str += '\t'; break;
                case 'u':  /* skip 4 hex */
                    if (i + 4 > s.size()) return fail("bad \\u");
                    /* For our needs we don't need to decode unicode. Skip. */
                    str += '?';
                    i += 4;
                    break;
                default: return fail("bad escape char");
                }
            } else {
                str += c;
            }
        }
        return fail("unterminated string");
    }

    bool parse_num(Value &out) {
        size_t start = i;
        if (s[i] == '-') ++i;
        while (!eof() && std::isdigit(static_cast<unsigned char>(s[i]))) ++i;
        /* No fraction support - we don't need it. */
        try {
            out = Value(static_cast<int64_t>(std::stoll(s.substr(start, i - start))));
        } catch (...) {
            return fail("bad number");
        }
        return true;
    }

    bool parse_bool(Value &out) {
        if (s.compare(i, 4, "true") == 0)  { i += 4; out = Value(true);  return true; }
        if (s.compare(i, 5, "false") == 0) { i += 5; out = Value(false); return true; }
        return fail("bad bool");
    }

    bool parse_null(Value &out) {
        if (s.compare(i, 4, "null") == 0) { i += 4; out = Value(); return true; }
        return fail("bad null");
    }
};

} // namespace

std::string encode(const Value &v) {
    std::ostringstream o;
    encode_v(o, v);
    return o.str();
}

bool decode(const std::string &s, Value &out, std::string &err) {
    Parser p(s);
    if (!p.parse(out)) { err = p.err; return false; }
    return true;
}

const std::string &obj_str(const Object &o, const std::string &k, const std::string &fallback) {
    auto it = o.find(k);
    if (it != o.end() && it->second.is_str()) return it->second.as_str();
    static thread_local std::string cache;
    cache = fallback;
    return cache;
}

int64_t obj_int(const Object &o, const std::string &k, int64_t fallback) {
    auto it = o.find(k);
    if (it != o.end() && it->second.is_int()) return it->second.as_int();
    return fallback;
}

const Array *obj_arr(const Object &o, const std::string &k) {
    auto it = o.find(k);
    if (it != o.end() && it->second.is_arr()) return &it->second.as_arr();
    return nullptr;
}

const Object *obj_obj(const Object &o, const std::string &k) {
    auto it = o.find(k);
    if (it != o.end() && it->second.is_obj()) return &it->second.as_obj();
    return nullptr;
}

} // namespace agentd::json
