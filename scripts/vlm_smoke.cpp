#include <chrono>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include "hailo/buffer.hpp"
#include "hailo/genai/vlm/vlm.hpp"
#include "hailo/hailort_defaults.hpp"
#include "hailo/vdevice.hpp"

namespace
{

std::vector<uint8_t> read_binary_file(const std::string &path)
{
    std::ifstream stream(path, std::ios::binary);
    if (!stream) {
        throw std::runtime_error("failed to open file: " + path);
    }

    stream.seekg(0, std::ios::end);
    const auto size = stream.tellg();
    stream.seekg(0, std::ios::beg);

    std::vector<uint8_t> data(static_cast<size_t>(size));
    if (!stream.read(reinterpret_cast<char *>(data.data()), size)) {
        throw std::runtime_error("failed to read file: " + path);
    }
    return data;
}

std::string status_to_string(hailort::genai::LLMGeneratorCompletion::Status status)
{
    using Status = hailort::genai::LLMGeneratorCompletion::Status;
    switch (status) {
    case Status::GENERATING:
        return "GENERATING";
    case Status::MAX_TOKENS_REACHED:
        return "MAX_TOKENS_REACHED";
    case Status::LOGICAL_END_OF_GENERATION:
        return "LOGICAL_END_OF_GENERATION";
    case Status::ABORTED:
        return "ABORTED";
    default:
        return "UNKNOWN";
    }
}

} // namespace

int main(int argc, char **argv)
{
    if (argc < 3) {
        std::cerr << "usage: " << argv[0] << " <vlm.hef> <rgb.raw> [prompt]\n";
        return 2;
    }

    const std::string hef_path = argv[1];
    const std::string image_path = argv[2];
    const std::string prompt = (argc >= 4)
        ? argv[3]
        : "この画像に写っているものを日本語で1文で説明してください。推測は書かないでください。";

    try {
        auto image = read_binary_file(image_path);

        auto vdevice_params = hailort::HailoRTDefaults::get_vdevice_params();
        auto vdevice = hailort::VDevice::create(vdevice_params).expect("failed to create VDevice");

        auto started_loading = std::chrono::steady_clock::now();
        hailort::genai::VLMParams vlm_params(hef_path);
        auto vlm = hailort::genai::VLM::create(vdevice, vlm_params).expect("failed to create VLM");
        auto finished_loading = std::chrono::steady_clock::now();

        const auto &shape = vlm.input_frame_shape();
        const auto expected_size = vlm.input_frame_size();
        std::cout << "input frame shape: " << shape.height << "x" << shape.width << "x" << shape.features << "\n";
        std::cout << "input frame size: " << expected_size << " bytes\n";
        std::cout << "raw image size: " << image.size() << " bytes\n";
        if (image.size() != expected_size) {
            std::cerr << "raw image size does not match VLM input_frame_size()\n";
            return 3;
        }

        auto generator_params = vlm.create_generator_params().expect("failed to create generator params");
        generator_params.set_max_generated_tokens(64);
        generator_params.set_do_sample(false);
        generator_params.set_temperature(0.0f);

        const std::string message =
            R"({"role":"user","content":[{"type":"image"},{"type":"text","text":")" + prompt + R"("}]})";

        std::vector<hailort::MemoryView> frames = {
            hailort::MemoryView(image.data(), image.size()),
        };
        std::vector<std::string> messages = {message};

        auto started_generation = std::chrono::steady_clock::now();
        auto completion = vlm.generate(generator_params, messages, frames).expect("failed to start generation");
        auto first_token_time = std::chrono::steady_clock::time_point{};
        std::string response;

        while (completion.generation_status() == hailort::genai::LLMGeneratorCompletion::Status::GENERATING) {
            auto token = completion.read(std::chrono::seconds(30)).expect("failed to read generated token");
            if (!token.empty() && first_token_time == std::chrono::steady_clock::time_point{}) {
                first_token_time = std::chrono::steady_clock::now();
            }
            response += token;
            std::cout << token << std::flush;
        }
        auto finished_generation = std::chrono::steady_clock::now();
        std::cout << "\n";

        const auto load_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            finished_loading - started_loading).count();
        const auto total_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            finished_generation - started_generation).count();
        const auto ttft_ms = (first_token_time == std::chrono::steady_clock::time_point{})
            ? -1
            : std::chrono::duration_cast<std::chrono::milliseconds>(first_token_time - started_generation).count();

        std::cout << "\n=== VLM smoke result ===\n";
        std::cout << "load_ms: " << load_ms << "\n";
        std::cout << "ttft_ms: " << ttft_ms << "\n";
        std::cout << "total_ms: " << total_ms << "\n";
        std::cout << "status: " << status_to_string(completion.generation_status()) << "\n";
        std::cout << "response_chars: " << response.size() << "\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "error: " << error.what() << "\n";
        return 1;
    }
}
