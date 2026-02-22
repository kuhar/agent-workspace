-- Tests for lsp module pure-function logic.
-- We test find_closest_symbol and _flatten_symbols without requiring vim.lsp.

local lsp = require("mark-and-recall.lsp")

describe("lsp", function()

  describe("_flatten_symbols", function()
    it("flattens nested symbols", function()
      local symbols = {
        { name = "outer", range = { start = { line = 0 }, ["end"] = { line = 20 } },
          children = {
            { name = "inner", range = { start = { line = 5 }, ["end"] = { line = 10 } } },
          }
        },
      }
      local flat = {}
      lsp._flatten_symbols(symbols, flat)
      assert_eq(#flat, 2)
      assert_eq(flat[1].name, "outer")
      assert_eq(flat[2].name, "inner")
    end)

    it("handles no children", function()
      local symbols = {
        { name = "a", range = { start = { line = 0 }, ["end"] = { line = 5 } } },
        { name = "b", range = { start = { line = 6 }, ["end"] = { line = 10 } } },
      }
      local flat = {}
      lsp._flatten_symbols(symbols, flat)
      assert_eq(#flat, 2)
    end)
  end)

  describe("find_closest_symbol", function()
    local function sym(name, start_line, end_line)
      return {
        name = name,
        range = { start = { line = start_line }, ["end"] = { line = end_line } },
      }
    end

    it("returns nil with no candidates", function()
      local symbols = { sym("foo", 0, 10), sym("bar", 15, 25) }
      assert_nil(lsp.find_closest_symbol(symbols, "baz", 5))
    end)

    it("returns nil with empty symbol list", function()
      assert_nil(lsp.find_closest_symbol({}, "foo", 5))
    end)

    it("returns nil with nil symbol list", function()
      assert_nil(lsp.find_closest_symbol(nil, "foo", 5))
    end)

    it("finds single matching symbol", function()
      local symbols = { sym("foo", 0, 10), sym("bar", 15, 25) }
      -- "foo" is at 0-based line 0 → 1-based line 1
      assert_eq(lsp.find_closest_symbol(symbols, "foo", 5), 1)
    end)

    it("picks closest when multiple matches", function()
      local symbols = {
        sym("handler", 0, 10),
        sym("handler", 50, 60),
        sym("handler", 100, 110),
      }
      -- Reference line 55 (1-based) → 54 (0-based), closest to symbol at line 50
      assert_eq(lsp.find_closest_symbol(symbols, "handler", 55), 51)
    end)

    it("picks first on tie-break (same distance)", function()
      local symbols = {
        sym("dup", 10, 20), -- distance from ref 16 (0-based 15): |10-15|=5
        sym("dup", 20, 30), -- distance: |20-15|=5
      }
      -- Both equidistant from line 16 (0-based 15), should pick first (line 10 → 1-based 11)
      assert_eq(lsp.find_closest_symbol(symbols, "dup", 16), 11)
    end)

    it("handles location-style symbols (sym.location.range)", function()
      local symbols = {
        { name = "foo", location = { range = { start = { line = 10 }, ["end"] = { line = 20 } } } },
      }
      assert_eq(lsp.find_closest_symbol(symbols, "foo", 15), 11)
    end)

    it("skips symbols with missing range", function()
      local symbols = {
        { name = "broken" }, -- no range or location
        sym("good", 5, 10),
      }
      assert_eq(lsp.find_closest_symbol(symbols, "good", 7), 6)
      assert_nil(lsp.find_closest_symbol(symbols, "broken", 1))
    end)
  end)

end)
