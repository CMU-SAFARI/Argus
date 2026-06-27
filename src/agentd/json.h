/* Minimal JSON for agentd's IPC. Supports the subset we need:
 *   - top-level object
 *   - string / int64 / bool / array values
 *   - escaping: \\ \" \n \t \r
 * Not a general-purpose JSON library; intentionally small.
 */
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <variant>
#include <vector>

namespace agentd::json {

struct Value;
using Array = std::vector<Value>;
using Object = std::map<std::string, Value>;

struct Value {
    std::variant<std::monostate, bool, int64_t, std::string, Array, Object> v;

    Value() = default;
    Value(bool b) : v(b) {}
    Value(int x) : v(static_cast<int64_t>(x)) {}
    Value(int64_t x) : v(x) {}
    Value(uint64_t x) : v(static_cast<int64_t>(x)) {}
    Value(const char *s) : v(std::string(s)) {}
    Value(std::string s) : v(std::move(s)) {}
    Value(Array a) : v(std::move(a)) {}
    Value(Object o) : v(std::move(o)) {}

    bool is_null() const { return std::holds_alternative<std::monostate>(v); }
    bool is_bool() const { return std::holds_alternative<bool>(v); }
    bool is_int() const { return std::holds_alternative<int64_t>(v); }
    bool is_str() const { return std::holds_alternative<std::string>(v); }
    bool is_arr() const { return std::holds_alternative<Array>(v); }
    bool is_obj() const { return std::holds_alternative<Object>(v); }

    bool as_bool() const { return std::get<bool>(v); }
    int64_t as_int() const { return std::get<int64_t>(v); }
    const std::string &as_str() const { return std::get<std::string>(v); }
    const Array &as_arr() const { return std::get<Array>(v); }
    const Object &as_obj() const { return std::get<Object>(v); }
};

std::string encode(const Value &v);
/* Returns true on success; on failure, msg holds the parse error. */
bool decode(const std::string &s, Value &out, std::string &err);

/* Convenience: read a string/int field from an object, or fall back. */
const std::string &obj_str(const Object &o, const std::string &k, const std::string &fallback = "");
int64_t obj_int(const Object &o, const std::string &k, int64_t fallback = 0);
const Array *obj_arr(const Object &o, const std::string &k);
const Object *obj_obj(const Object &o, const std::string &k);

} // namespace agentd::json
