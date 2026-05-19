from audio_spider.pa_backend import format_module_args, format_proplist


class TestFormatModuleArgs:
    def test_simple_pair(self):
        assert format_module_args({"sink_name": "vmic1"}) == "sink_name=vmic1"

    def test_skips_none_values(self):
        assert format_module_args({"a": None, "b": "x"}) == "b=x"

    def test_quotes_value_with_space(self):
        assert format_module_args({"source": "mic with space"}) == 'source="mic with space"'

    def test_escapes_inner_double_quotes(self):
        result = format_module_args({"desc": 'has "quote"'})
        assert result == 'desc="has \\"quote\\""'

    def test_preserves_order(self):
        result = format_module_args({"first": "1", "second": "2", "third": "3"})
        assert result == "first=1 second=2 third=3"

    def test_empty_dict_yields_empty_string(self):
        assert format_module_args({}) == ""


class TestFormatProplist:
    def test_simple_key_value(self):
        assert format_proplist({"device.description": "MyDevice"}) == "device.description=MyDevice"

    def test_wraps_value_with_space_in_single_quotes(self):
        assert format_proplist({"device.description": "Audio Spider Test"}) == \
            "device.description='Audio Spider Test'"

    def test_multiple_props_comma_separated(self):
        result = format_proplist({"a": "x", "b": "with space"})
        assert result == "a=x,b='with space'"

    def test_escapes_inner_single_quote(self):
        result = format_proplist({"k": "what's up"})
        assert result == "k='what\\'s up'"

    def test_wraps_value_with_comma(self):
        # comma is the proplist separator → must be escaped via quoting
        assert format_proplist({"k": "a,b"}) == "k='a,b'"

    def test_wraps_value_with_equals(self):
        assert format_proplist({"k": "x=y"}) == "k='x=y'"
