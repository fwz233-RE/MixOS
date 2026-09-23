// Host-only regression against term-ime's actual staged input state machine.
#include "input_processor.hpp"
#include "mix_input.h"
#include "ime_hint_host.hpp"
#include <cassert>
#include <iostream>
#include <string>

static void forward(InputProcessor& processor, const std::string& bytes) {
    std::string result;
    for (unsigned char byte : bytes) {
        auto event = processor.process(byte);
        assert(!event.toggle_mode);
        if (event.forward) result.append(event.data.begin(), event.data.end());
    }
    assert(result == bytes);
}

int main(int argc, char**) {
    if (argc > 1) {
        std::cout << ImeToggleHint();
        return 0;
    }
    const std::string shortcut = MIX_IME_TOGGLE_SEQUENCE;
    // A read may end at any byte. State lives in InputProcessor, never in the
    // caller's read buffer; even one byte per call produces one final action.
    for (size_t split = 0; split <= shortcut.size(); ++split) {
        InputProcessor processor;
        int toggles = 0;
        for (const auto& chunk : {shortcut.substr(0, split), shortcut.substr(split)}) {
            for (unsigned char byte : chunk) {
                auto event = processor.process(byte);
                assert(!event.forward && event.data.empty());
                if (event.toggle_mode) ++toggles;
            }
        }
        assert(toggles == 1);
        forward(processor, " nihao\x13\x11");
    }
    InputProcessor processor;
    for (int repeat = 0; repeat < 2; ++repeat) {
        auto prefix = processor.process(1);
        assert(!prefix.forward && !prefix.toggle_mode);
        auto toggle = processor.process(' ');
        assert(toggle.toggle_mode && !toggle.forward && toggle.data.empty());
    }
    // Near-miss modifiers, arrows, ordinary Space and Ctrl+A settings retain
    // their upstream semantics; only the exact Shift+Space CSI is consumed.
    forward(processor, "\x1b[32;3u");
    forward(processor, "\x1b[32;6u");
    forward(processor, "\x1b[1;2A");
    forward(processor, "\x1bOA");
    forward(processor, "\x01s");
    forward(processor, " ");
    processor.process(1);
    auto literal = processor.process(1);
    assert(literal.forward && literal.data == std::vector<uint8_t>{1});
    processor.process(0x1b);
    processor.process('[');
    processor.reset();
    forward(processor, "q");
    std::cout << "term-ime: CSI toggle, split input, legacy keys passed\n";
}
