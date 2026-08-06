// Sample C# file exercising many constructs.
using System;
using System.Collections.Generic;

namespace SampleApp.Models;

/// <summary>A base class for animals.</summary>
public abstract class Animal
{
    public string Name { get; set; }

    public int Age { get; private set; }

    public Animal(string name)
    {
        Name = name;
    }

    public abstract string Speak();

    public virtual string Describe()
    {
        return $"{Name} is {Age} years old";
    }
}

public interface IFlyable
{
    void Fly();
}

public sealed class Dog : Animal, IFlyable
{
    public Dog(string name) : base(name)
    {
    }

    public override string Speak() => "Woof";

    public void Fly()
    {
        int distance = 10;
        while (distance > 100)
        {
            distance /= 2;
        }
        for (int i = 0; i < 3; i++)
        {
            distance += i;
        }
        if (distance > 10)
        {
            Console.WriteLine("far");
        }
        else
        {
            Console.WriteLine("near");
        }
        switch (distance)
        {
            case 1:
                Console.WriteLine("one");
                break;
            default:
                Console.WriteLine("many");
                break;
        }
        try
        {
            var parts = Name.Split(' ');
        }
        catch (ArgumentNullException ex) when (ex.ParamName is not null)
        {
            throw new InvalidOperationException("bad name", ex);
        }
        finally
        {
            Console.WriteLine("done");
        }
#if DEBUG
        Console.WriteLine("debug build");
#endif
    }
}

public enum AnimalKind
{
    Mammal,
    Reptile,
    Bird
}

public struct Point
{
    public int X { get; set; }
    public int Y { get; set; }
}

public static class Helpers
{
    public static T FirstOrDefault<T>(this IEnumerable<T> source, Func<T, bool> predicate)
    {
        foreach (var item in source)
        {
            if (predicate(item))
            {
                return item;
            }
        }
        return default;
    }
}
