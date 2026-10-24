#include <enttec/usb_client.h>
#include <enttec/libenttec/usb_pro.h>
#include <core/clock.h>
#include <vector>
#include <array>
#include <cstring>
#include <stdexcept>
#include <iostream>
using namespace std::chrono_literals;

namespace {

std::vector<unsigned char> input, lastWrite;
size_t readAt = 0;
bool writeFailure = false, shortWrite = false;
int closes = 0;

void Check(bool ok, const char* reason)
{
	if (!ok)
		throw std::runtime_error(reason);
}

void Reply(std::initializer_list<unsigned char> bytes)
{
	input = bytes;
	readAt = 0;
}

} // namespace

extern "C" FT_STATUS WINAPI FT_Open(int, FT_HANDLE* handle)
{
	*handle = reinterpret_cast<FT_HANDLE>(1);
	return FT_OK;
}

extern "C" FT_STATUS WINAPI FT_Close(FT_HANDLE)
{
	++closes;
	return FT_OK;
}

extern "C" FT_STATUS WINAPI FT_SetTimeouts(FT_HANDLE, ULONG, ULONG)
{
	return FT_OK;
}

extern "C" FT_STATUS WINAPI FT_Purge(FT_HANDLE, ULONG)
{
	return FT_OK;
}

extern "C" FT_STATUS WINAPI FT_Reload(WORD, WORD)
{
	return FT_OK;
}

extern "C" FT_STATUS WINAPI FT_ListDevices(PVOID count, PVOID, DWORD)
{
	*static_cast<DWORD*>(count) = 1;
	return FT_OK;
}

extern "C" FT_STATUS WINAPI FT_Write(FT_HANDLE, LPVOID data, DWORD size, LPDWORD written)
{
	auto* bytes = static_cast<unsigned char*>(data);
	lastWrite.assign(bytes, bytes + size);
	*written = shortWrite ? size - 1 : size;
	if (size >= 2 && bytes[1] == GET_WIDGET_PARAMS)
		Reply({0x7e, 3, 5, 0, 1, 2, 3, 4, 5, 0xe7});
	return writeFailure ? FT_IO_ERROR : FT_OK;
}

extern "C" FT_STATUS WINAPI FT_Read(FT_HANDLE, LPVOID data, DWORD size, LPDWORD received)
{
	const auto count = (std::min)(size, static_cast<DWORD>(input.size() - readAt));
	std::memcpy(data, input.data() + readAt, count);
	readAt += count;
	*received = count;
	return FT_OK;
}

int main()
{
	Check(FTDI_OpenDevice(0) == TRUE, "open valid USB device");
	unsigned char data[513]{};
	Check(FTDI_SendData(6, data, 513) == TRUE && lastWrite.size() == 518, "whole DMX packet");
	Check(lastWrite.front() == 0x7e && lastWrite.back() == 0xe7 && lastWrite[2] == 1 && lastWrite[3] == 2,
	      "USB framing");
	writeFailure = true;
	Check(FTDI_SendData(6, data, 513) == NO_RESPONSE, "driver error with full byte count");
	writeFailure = false;
	shortWrite = true;
	Check(FTDI_SendData(6, data, 513) == NO_RESPONSE, "short USB write");
	shortWrite = false;
	Reply({0x7e, 3, 2, 0, 1, 2, 0xe7});
	data[0] = 42;
	Check(!FTDI_ReceiveData(3, data, 5) && data[0] == 42, "short reply must not copy uninitialized bytes");
	Check(!FTDI_ReceiveData(3, data, 514), "oversized expected reply");
	FTDI_ClosePort();
	const int closed = closes;
	FTDI_ClosePort();
	Check(closes == closed, "double close");
	Radius::EventLoop loop;
	Radius::EnttecUSBClient usb(loop);
	usb.Start();
	Check(lastWrite[1] == SET_DMX_TX_MODE && lastWrite[4] == 0,
	      "startup is a DMX blackout, never a widget-settings write");
	std::array<uint8_t, 512> frame{};
	frame[0] = 255;
	usb.UpdateFrame(Radius::DmxFrame(frame));
	writeFailure = true;
	auto deadline = loop.AddTimer(500ms, [&] {
		loop.Stop();
	});
	bool failed = false;
	try
	{
		loop.Run(Radius::RunHot::No);
	}
	catch (const std::runtime_error&)
	{
		failed = true;
	}
	Check(failed && closes == closed + 1, "USB output failure must stop the worker and close the device");
	std::cout << "USB framing, short reads/writes, driver failure, startup blackout and close ownership passed\n";
}
