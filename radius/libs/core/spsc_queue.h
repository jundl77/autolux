#pragma once
#include <array>
#include <atomic>
#include <cstddef>

namespace Radius {
// One producer, one consumer. Neither side may modify the other side's cursor.
template<class T, size_t Capacity> class SpscQueue {
    static_assert(Capacity > 1);
    std::array<T, Capacity> mItems {};
    alignas(64) std::atomic<size_t> mWrite {0};
    alignas(64) std::atomic<size_t> mRead {0};
public:
    bool Push(const T& value) noexcept {
        const auto write = mWrite.load(std::memory_order_relaxed);
        if (write - mRead.load(std::memory_order_acquire) == Capacity) return false;
        mItems[write % Capacity] = value;
        mWrite.store(write + 1, std::memory_order_release);
        return true;
    }
    const T* Front() const noexcept {
        const auto read = mRead.load(std::memory_order_relaxed);
        if (read == mWrite.load(std::memory_order_acquire)) return nullptr;
        return &mItems[read % Capacity];
    }
    void Pop() noexcept { mRead.fetch_add(1, std::memory_order_release); }
    size_t Size() const noexcept {
        const auto read = mRead.load(std::memory_order_acquire);
        const auto write = mWrite.load(std::memory_order_acquire);
        return write - read;
    }
};
}
