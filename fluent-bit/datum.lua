local MAX_MESSAGE_BYTES = 8192
local MAX_NAME_LENGTH = 200
local MAX_ATTRIBUTES = 64
local UNKNOWN = "unknown"
local DEFAULT_LEVEL = "info"

local MESSAGE_KEYS = { "message", "msg", "MESSAGE", "log" }
local LEVEL_KEYS = { "level", "severity", "lvl", "PRIORITY" }
local SERVICE_KEYS = { "service", "SYSLOG_IDENTIFIER", "_SYSTEMD_UNIT" }
local SOURCE_KEYS = { "source", "filename", "_SYSTEMD_UNIT" }

local SYSLOG_LEVELS = {
    ["0"] = "emerg", ["1"] = "alert", ["2"] = "crit", ["3"] = "error",
    ["4"] = "warning", ["5"] = "notice", ["6"] = "info", ["7"] = "debug",
}

local function first_present(record, keys)
    for _, key in ipairs(keys) do
        if record[key] ~= nil then
            return record[key], key
        end
    end
end

local function is_continuation_byte(byte)
    return byte ~= nil and byte >= 0x80 and byte < 0xC0
end

local function truncate_utf8(text, limit)
    if #text <= limit then
        return text
    end
    local cut = limit
    while cut > 0 and is_continuation_byte(text:byte(cut + 1)) do
        cut = cut - 1
    end
    return text:sub(1, cut)
end

local function log_name(value)
    if value == nil then
        return UNKNOWN
    end
    local name = tostring(value):gsub("[^%w_./-]", "_"):sub(1, MAX_NAME_LENGTH)
    return name ~= "" and name or UNKNOWN
end

local function source_name(record)
    local source, key = first_present(record, SOURCE_KEYS)
    if key == "filename" then
        source = tostring(source):match("[^/]+$")
    end
    return log_name(source)
end

local function identifier(value)
    local name = tostring(value):gsub("[^%w_]", "_")
    if name:match("^%d") then
        name = "_" .. name
    end
    return name:sub(1, MAX_NAME_LENGTH)
end

local function level_name(value)
    if value == nil then
        return DEFAULT_LEVEL
    end
    local level = tostring(value):lower()
    level = SYSLOG_LEVELS[level] or level
    if level:match("^[%a_][%w_]*$") and #level <= MAX_NAME_LENGTH then
        return level
    end
    return DEFAULT_LEVEL
end

local function is_scalar(value)
    local kind = type(value)
    return kind == "string" or kind == "number" or kind == "boolean"
end

local function attributes_of(record, used)
    local keys = {}
    for key, value in pairs(record) do
        if not used[key] and is_scalar(value) then
            keys[#keys + 1] = key
        end
    end
    table.sort(keys)

    local attributes, count = {}, 0
    for _, key in ipairs(keys) do
        if count == MAX_ATTRIBUTES then
            break
        end
        local name = identifier(key)
        if name ~= "" and attributes[name] == nil then
            attributes[name] = tostring(record[key])
            count = count + 1
        end
    end
    if count == 0 then
        return nil
    end
    return attributes
end

function datum_line(tag, timestamp, record)
    local message, message_key = first_present(record, MESSAGE_KEYS)
    local level, level_key = first_present(record, LEVEL_KEYS)
    local used = { product = true, service = true, source = true, ts = true }
    if message_key then used[message_key] = true end
    if level_key then used[level_key] = true end

    local line = {
        product = log_name(record.product),
        service = log_name(first_present(record, SERVICE_KEYS)),
        source = source_name(record),
        level = level_name(level),
        message = truncate_utf8(message ~= nil and tostring(message) or "", MAX_MESSAGE_BYTES),
        attributes = attributes_of(record, used),
    }
    return 2, timestamp, line
end
