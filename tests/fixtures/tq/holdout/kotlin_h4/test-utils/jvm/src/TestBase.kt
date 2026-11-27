package example.testing

val isStressTest = false
actual val stressTestMultiplierSqrt = if (isStressTest) 5 else 1
