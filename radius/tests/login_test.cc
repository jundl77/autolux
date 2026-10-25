#include "../radius_rx/radius_rx.h"
#include "../radius_tx/radius_tx.h"
#include <stdexcept>

using namespace Radius;
using namespace std::chrono_literals;

namespace {

constexpr uint64_t TestSession = UINT64_MAX;
constexpr auto TestDeadline = 3s;
constexpr auto PollInterval = 1ms;
constexpr uint32_t TestJitterMilliseconds = 75;
constexpr uint64_t RetryTimingToleranceMilliseconds = 20;

void Require(bool condition)
{
	if (!condition)
		throw std::runtime_error("login lifecycle regression failed");
}

Config TestConfig()
{
	Config config;
	config.mBindAddress = "127.0.0.1";
	config.mPeerAddress = "127.0.0.1";
	config.mSession = TestSession;
	config.mAudioEnabled = config.mDmxEnabled = config.mArtnetEnabled = false;
	config.mReceiverAudioEnabled = config.mReceiverLightingEnabled = false;
	SOCKET socketHandle = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	Require(socketHandle != INVALID_SOCKET);
	sockaddr_in address{};
	address.sin_family = AF_INET;
	address.sin_addr.s_addr = inet_addr("127.0.0.1");
	Require(bind(socketHandle, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
	int length = sizeof(address);
	Require(getsockname(socketHandle, reinterpret_cast<sockaddr*>(&address), &length) == 0);
	config.mBusPort = ntohs(address.sin_port);
	closesocket(socketHandle);
	return config;
}

void CheckReceiverLogin()
{
	auto config = TestConfig();
	EventLoop loop;
	RadiusRx receiver(config, loop);
	receiver.Start();
	BusSocket client;
	client.Open(config.mBindAddress, config.mPeerAddress, config.mBusPort, false);
	Bus::Login login;
	login.set_command_id(Bus::LoginCommandId);
	login.set_session_id(TestSession);
	login.set_protocol_major(Bus::ProtocolMajor + 1);
	login.set_protocol_minor(Bus::ProtocolMinor + 1);
	login.set_sample_rate_hz(Bus::SampleRateHz);
	login.set_channel_count(Bus::Channels);
	login.set_jitter_buffer_ms(TestJitterMilliseconds);
	Require(client.Send(login));
	unsigned replies = 0, errors = 0;
	bool complete = false;
	auto timeout = loop.AddTimer(TestDeadline, [&] {
		loop.Stop();
	});
	auto poll = loop.AddTimer(PollInterval, [&] {
		client.Poll(TestSession, [&](const Bus::Message& message) {
			if (const auto* error = std::get_if<Bus::ErrorReply>(&message))
			{
				Require(error->command_id() == login.command_id());
				++errors;
				if (errors == 1)
				{
					Require(error->message().find("major") != std::string::npos);
					login.set_protocol_major(Bus::ProtocolMajor);
					Require(client.Send(login));
				}
				else
				{
					Require(errors == 2 && replies == 2);
					Require(error->message().find("already") != std::string::npos);
					Require(receiver.GetConfig().mJitterBufferMs == TestJitterMilliseconds);
					Bus::Heartbeat heartbeat;
					heartbeat.set_session_id(TestSession);
					Require(client.Send(heartbeat));
				}
			}
			else if (const auto* reply = std::get_if<Bus::LoginReply>(&message))
			{
				Require(reply->command_id() == login.command_id());
				Require(reply->protocol_major() == Bus::ProtocolMajor);
				Require(reply->protocol_minor() == Bus::ProtocolMinor);
				Require(reply->max_message_bytes() == Bus::MaxDatagramBytes);
				++replies;
				if (replies == 1)
					Require(client.Send(login)); // A lost reply causes an identical retry.
				else
				{
					Require(replies == 2);
					login.set_jitter_buffer_ms(TestJitterMilliseconds + 1);
					Require(client.Send(login)); // No replacement of an initialized session.
				}
			}
			else if (const auto* metrics = std::get_if<Bus::Metrics>(&message);
			         metrics && errors == 2 && metrics->heartbeats_received() == 1)
			{
				Require(metrics->heartbeats_sent() == 1); // Receiving is not a heartbeat echo.
				Require(metrics->last_processed_command_id() == Bus::LoginCommandId);
				Require(metrics->buffer_target_seconds() == TestJitterMilliseconds / double(MillisecondsPerSecond));
				complete = true;
				loop.Stop();
			}
		});
	});
	loop.Run(RunHot::No);
	Require(complete && errors == 2 && replies == 2);
}

void CheckTransmitterLoginRetry()
{
	auto config = TestConfig();
	EventLoop loop;
	BusSocket receiver;
	receiver.Open(config.mBindAddress, config.mPeerAddress, config.mBusPort, true);
	RadiusTx transmitter(config, loop);
	transmitter.Start();
	Require(!transmitter.IsReady());
	unsigned logins = 0;
	bool complete = false;
	uint64_t firstLoginAt = 0;
	std::string originalLogin;
	auto timeout = loop.AddTimer(TestDeadline, [&] {
		loop.Stop();
	});
	auto poll = loop.AddTimer(PollInterval, [&] {
		receiver.Poll(TestSession, [&](const Bus::Message& message) {
			if (const auto* login = std::get_if<Bus::Login>(&message))
			{
				Require(!transmitter.IsReady());
				++logins;
				if (logins == 1)
				{
					originalLogin = login->SerializeAsString();
					firstLoginAt = SteadyMs();
					return; // Simulate loss of the first login/reply exchange.
				}
				Require(login->SerializeAsString() == originalLogin);
				Require(SteadyMs() - firstLoginAt + RetryTimingToleranceMilliseconds >= Bus::LoginRetryMilliseconds);
				Bus::LoginReply reply;
				reply.set_session_id(TestSession);
				reply.set_command_id(login->command_id());
				reply.set_protocol_major(Bus::ProtocolMajor);
				reply.set_protocol_minor(Bus::ProtocolMinor);
				reply.set_max_message_bytes(Bus::MaxDatagramBytes);
				Require(receiver.Send(reply));
			}
			else if (std::holds_alternative<Bus::Heartbeat>(message))
			{
				Require(transmitter.IsReady() && logins == 2);
				complete = true;
				loop.Stop();
			}
			else
				Require(false); // No capture or lighting transmission before LoginReply.
		});
	});
	loop.Run(RunHot::No);
	Require(complete);
}

} // namespace

void CheckLoginLifecycle()
{
	CheckReceiverLogin();
	CheckTransmitterLoginRetry();
}
