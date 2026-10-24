#include "radius_rx.h"

int main(int argc, char* argv[])
{
	return Radius::AppMain<Radius::RadiusRx>(argc, argv, "radius_rx", Radius::AppType::Rx);
}
